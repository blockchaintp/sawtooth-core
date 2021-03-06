# Copyright 2017 Intel Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ------------------------------------------------------------------------------

import logging
from threading import RLock
from collections import deque

import os
import diskcache

from sawtooth_validator.journal.block_manager import MissingPredecessor
from sawtooth_validator.journal.block_wrapper import BlockWrapper
from sawtooth_validator.journal.timed_cache import TimedCache
from sawtooth_validator.protobuf.transaction_pb2 import TransactionHeader
from sawtooth_validator.protobuf.block_pb2 import Block
from sawtooth_validator.protobuf import network_pb2
from sawtooth_validator.networking.dispatch import Handler
from sawtooth_validator.networking.dispatch import HandlerResult
from sawtooth_validator.networking.dispatch import HandlerStatus
from sawtooth_validator import metrics

LOGGER = logging.getLogger(__name__)
COLLECTOR = metrics.get_collector(__name__)


class Completer:
    """
    The Completer is responsible for making sure blocks are formally
    complete before they are delivered to the chain controller. A formally
    complete block is a block whose predecessor is in the block cache and all
    the batches are present in the batch list and in the order specified by the
    block header. If the predecessor or a batch is missing, a request message
    is sent sent out over the gossip network. It also checks that all batches
    have their dependencies satisifed, otherwise it will request the batch that
    has the missing transaction.
    """

    def __init__(self,
                 block_manager,
                 transaction_committed,
                 get_committed_batch_by_id,
                 get_committed_batch_by_txn_id,
                 gossip,
                 data_dir,
                 cache_keep_time=10,
                 cache_purge_frequency=30,
                 requested_keep_time=300):
        """
        :param block_manager (BlockManager) An object for getting and storing
            blocks safely
        :param transaction_committed (fn(transaction_id) -> bool) A function to
            determine if a transaction is committed.
        :param batch_committed (fn(batch_id) -> bool) A function to
            determine if a batch is committed.
        :param get_committed_batch_by_txn_id
            (fn(transaction_id) -> Batch) A function for retrieving a committed
            batch from a committed transction id.
        :param gossip (gossip.Gossip) Broadcasts block and batch request to
                peers
        :param data_dir (string) The directory to store data in
        :param cache_keep_time (float) Time in seconds to keep values in
            TimedCaches.
        :param cache_purge_frequency (float) Time between purging the
            TimedCaches.
        :param requested_keep_time (float) Time in seconds to keep the ids
            of requested objects. WARNING this time should always be less than
            cache_keep_time or the validator can get into a state where it
            fails to make progress because it thinks it has already requested
            something that it is missing.
        """
        self._gossip = gossip
        self._batch_cache = TimedCache(cache_keep_time, cache_purge_frequency)
        self._block_manager = block_manager

        self._transaction_committed = transaction_committed
        self._get_committed_batch_by_id = get_committed_batch_by_id
        self._get_committed_batch_by_txn_id = get_committed_batch_by_txn_id

        self._seen_txns = TimedCache(cache_keep_time, cache_purge_frequency)
        self._incomplete_batches = TimedCache(cache_keep_time,
                                              cache_purge_frequency)
        self._incomplete_blocks = TimedCache(cache_keep_time,
                                             cache_purge_frequency)
        self._disk_blocks = diskcache.Cache(os.path.join(data_dir, "catchup"),
                                            eviction_policy='none',
                                            cull_limit=0)
        self._raw_blocks = diskcache.Cache(os.path.join(data_dir, "raw"),
                                           eviction_policy='none',
                                           cull_limit=0)
        self._knit_complete = False
        self._requested = TimedCache(requested_keep_time,
                                     cache_purge_frequency)
        self._get_chain_head = None
        self._on_block_received = None
        self._on_batch_received = None
        self.lock = RLock()

        # Tracks how many times an unsatisfied dependency is found
        self._unsatisfied_dependency_count = COLLECTOR.counter(
            'unsatisfied_dependency_count', instance=self)
        # Tracks the length of the completer's _seen_txns
        self._seen_txns_length = COLLECTOR.gauge(
            'seen_txns_length', instance=self)
        self._seen_txns_length.set_value(0)
        # Tracks the length of the completer's _incomplete_blocks
        self._incomplete_blocks_length = COLLECTOR.gauge(
            'incomplete_blocks_length', instance=self)
        self._incomplete_blocks_length.set_value(0)
        # Tracks the length of the completer's _incomplete_batches
        self._incomplete_batches_length = COLLECTOR.gauge(
            'incomplete_batches_length', instance=self)
        self._incomplete_batches_length.set_value(0)

    def _put_or_request_if_missing_predecessor(self, blkw):
        try:
            # Create Ref-B
            LOGGER.debug("BlockManager Put: %s", blkw)
            self._block_manager.put([blkw.block])
            return blkw
        except MissingPredecessor:
            # The predecessor dropped out of the block manager between when we
            # checked if it was there and when the block was determined to be
            # complete.
            LOGGER.debug("Missing predecessor for block: %s", blkw)
            return self._request_previous_if_not_already_requested(blkw)

    def _cache_block(self, blkw):
        # use some local variables to share the logic
        cache = self._incomplete_blocks
        value = blkw

        # This checks if the block is a parent (since a child requested it)
        # and therefore stable and disk cacheable
        cache_to_disk = "memory"
        if blkw.header_signature in self._requested:
            cache = self._disk_blocks
            cache_to_disk = "disk"
            value = blkw.block.SerializeToString()
            if blkw.header_signature not in self._raw_blocks:
                LOGGER.debug("Raw caching block: %s", blkw)
                self._raw_blocks[blkw.header_signature] = value

        # this is a child block, may be multiple of them due to a fork
        if blkw.previous_block_id not in cache:
            LOGGER.debug("Caching block: %s as child to %s", blkw, cache_to_disk)
            cache[blkw.previous_block_id] = [value]
        elif value not in cache[blkw.previous_block_id]:
            LOGGER.debug("Caching block: %s as sibling to %s", blkw, cache_to_disk)
            cache[blkw.previous_block_id] += [value]

    def _knit_cache(self):
        with self.lock:
            if self._knit_complete:
                return

            count = 0
            updates = 0
            LOGGER.debug("Begin knitting cache")
            for block_id in self._raw_blocks:
                raw_data = self._raw_blocks[block_id]
                current_blkw = self._wrap_block(raw_data)[1]
                if current_blkw.previous_block_id not in self._disk_blocks:
                    self._disk_blocks[current_blkw.previous_block_id] = [raw_data]
                    updates += 1
                elif raw_data not in self._disk_blocks[current_blkw.previous_block_id]:
                    self._disk_blocks[current_blkw.previous_block_id] += [raw_data]
                    updates += 1
                count += 1
                if count % 10000 == 0:
                    LOGGER.debug("Knitted %s blocks so far, updates: %s", count, updates)
            self._knit_complete = True
            LOGGER.debug("Knit %s blocks into cache with %s updates", count, updates)

    def _clean_cache(self, blkw):
        with self.lock:
            count = 0
            cache_blkw = blkw
            while cache_blkw.header_signature in self._raw_blocks:
                del self._raw_blocks[cache_blkw.header_signature]
                count += 1
                if cache_blkw.previous_block_id in self._raw_blocks:
                    raw_data = self._raw_blocks[cache_blkw.previous_block_id]
                    cache_blkw = self._wrap_block(raw_data)[1]
                else:
                    break
            if count > 0:
                LOGGER.debug("Cleaned %s entries from raw cache", count)

    def _request_previous_if_not_already_requested(self, blkw):
        # use some local variables to share the logic
        self._cache_block(blkw)

        # We have already requested the block, do not do so again
        if blkw.previous_block_id in self._requested:
            LOGGER.debug(
                "Skip repeat request missing predecessor of block: %s", blkw)
            return None
        else:
            cached_blkw = blkw
            previous_blkw = None
            descents = 0
            chain_head = self._get_chain_head()
            chain_head_blkw = None
            if chain_head:
                chain_head_blkw = BlockWrapper(chain_head)
                self._clean_cache(chain_head_blkw)
                self._knit_cache()

            while cached_blkw.previous_block_id in self._raw_blocks:
                if chain_head_blkw and chain_head_blkw.header_signature == cached_blkw.previous_block_id:
                    cached_blkw = previous_blkw
                    break
                else:
                    self._requested[cached_blkw.previous_block_id] = None
                    previous_blkw = cached_blkw
                    cached_data = self._raw_blocks[cached_blkw.previous_block_id]
                    cached_blkw = self._wrap_block(cached_data)[1]
                    descents = descents + 1

            if cached_blkw.previous_block_id not in self._requested:
                LOGGER.debug("Request missing predecessor of block: %s descents=%s",
                            cached_blkw, descents)
                self._requested[cached_blkw.previous_block_id] = None
                self._gossip.broadcast_block_request(cached_blkw.previous_block_id)
            return None

    def _complete_block(self, block):
        """ Check the block to see if it is complete and if it can be passed to
            the journal. If the block's predecessor is not in the block_manager
            the predecessor is requested and the current block is added to the
            the incomplete_block cache. If the block.batches and
            block.header.batch_ids are not the same length, the batch_id list
            is checked against the batch_cache to see if the batch_list can be
            built. If any batches are missing from the block and we do not have
            the batches in the batch_cache, they are requested. The block is
            then added to the incomplete_block cache. If we can complete the
            block, a new batch list is created in the correct order and added
            to the block. The block is now considered complete and is returned.
            If block.batches and block.header.batch_ids are the same length,
            the block's batch list needs to be in the same order as the
            block.header.batch_ids list. If the block has all of its expected
            batches but are not in the correct order, the batch list is rebuilt
            and added to the block. Once a block has the correct batch list it
            is added to the block_manager and is returned.

        """

        if block.header_signature in self._block_manager:
            LOGGER.debug("Drop duplicate block: %s", block)
            return None

        # NOTE: We cannot assume that if the previous block _is_ in the block
        # manager, that it will still be in there when this block is complete.
        if block.previous_block_id not in self._block_manager:
            return self._request_previous_if_not_already_requested(block)

        LOGGER.debug("CompleteBlock Check: %s inc_block %d inc_batch %d",
                     block, len(self._incomplete_blocks),
                     len(self._incomplete_batches))

        # Check for same number of batch_ids and batches
        # If different starting building batch list, Otherwise there is a batch
        # that does not belong, block should be dropped.
        if len(block.batches) > len(block.header.batch_ids):
            LOGGER.debug("Block has extra batches. Dropping %s", block)
            return None

        # used to supplement batch_cache, contains batches already in block
        temp_batches = {}
        for batch in block.batches:
            temp_batches[batch.header_signature] = batch

        # The block is missing batches. Check to see if we can complete it.
        if len(block.batches) != len(block.header.batch_ids):
            building = True
            LOGGER.debug("Batches missing: have %d expected %d", len(block.batches),
                         len(block.header.batch_ids))
            for batch_id in block.header.batch_ids:
                if batch_id not in self._batch_cache and \
                        batch_id not in temp_batches:
                    # Request all missing batches
                    if batch_id not in self._incomplete_blocks:
                        self._incomplete_blocks[batch_id] = [block]
                    elif block not in self._incomplete_blocks[batch_id]:
                        self._incomplete_blocks[batch_id] += [block]

                    # We have already requested the batch, do not do so again
                    if batch_id in self._requested:
                        return None
                    self._requested[batch_id] = None
                    self._gossip.broadcast_batch_by_batch_id_request(batch_id)
                    building = False

            # The block cannot be completed.
            if not building:
                return None

            batches = self._finalize_batch_list(block, temp_batches)
            del block.batches[:]
            # reset batches with full list batches
            block.batches.extend(batches)
            if block.header_signature in self._requested:
                del self._requested[block.header_signature]

            return self._put_or_request_if_missing_predecessor(block)

        batch_id_list = [x.header_signature for x in block.batches]
        # Check to see if batchs are in the correct order.
        if batch_id_list == list(block.header.batch_ids):
            if block.header_signature in self._requested:
                del self._requested[block.header_signature]

            return self._put_or_request_if_missing_predecessor(block)

        # Check to see if the block has all batch_ids and they can be put
        # in the correct order
        if sorted(batch_id_list) == sorted(list(block.header.batch_ids)):
            batches = self._finalize_batch_list(block, temp_batches)
            # Clear batches from block
            del block.batches[:]
            # reset batches with full list batches
            if batches is not None:
                block.batches.extend(batches)
            else:
                return None

            if block.header_signature in self._requested:
                del self._requested[block.header_signature]

            return self._put_or_request_if_missing_predecessor(block)

        LOGGER.debug("Block.header.batch_ids does not match set of "
                     "batches in block.batches Dropping %s", block)
        return None

    def _finalize_batch_list(self, block, temp_batches):
        batches = []
        for batch_id in block.header.batch_ids:
            if batch_id in self._batch_cache:
                batches.append(self._batch_cache[batch_id])
            elif batch_id in temp_batches:
                batches.append(temp_batches[batch_id])
            else:
                return None

        return batches

    def _complete_batch(self, batch):
        valid = True
        dependencies = []
        for txn in batch.transactions:
            txn_header = TransactionHeader()
            txn_header.ParseFromString(txn.header)
            for dependency in txn_header.dependencies:
                # Check to see if the dependency has been seen or is committed
                if dependency not in self._seen_txns and not \
                        self._transaction_committed(dependency):
                    self._unsatisfied_dependency_count.inc()

                    # Check to see if the dependency has already been requested
                    if dependency not in self._requested:
                        dependencies.append(dependency)
                        self._requested[dependency] = None
                    if dependency not in self._incomplete_batches:
                        self._incomplete_batches[dependency] = [batch]
                    elif batch not in self._incomplete_batches[dependency]:
                        self._incomplete_batches[dependency] += [batch]
                    valid = False
        if not valid:
            self._gossip.broadcast_batch_by_transaction_id_request(
                dependencies)

        return valid

    def _add_seen_txns(self, batch):
        for txn in batch.transactions:
            self._seen_txns[txn.header_signature] = batch.header_signature
            self._seen_txns_length.set_value(
                len(self._seen_txns))

    def _process_incomplete_batches(self, key):
        # Keys are transaction_id
        if key in self._incomplete_batches:
            batches = self._incomplete_batches[key]
            for batch in batches:
                self.add_batch(batch)
            del self._incomplete_batches[key]

    def _process_incomplete_blocks(self, key):
        # Keys are either a block_id or batch_id
        if key in self._incomplete_blocks or key in self._disk_blocks:
            to_complete = deque()
            to_complete.append(key)

            while to_complete:
                my_key = to_complete.popleft()
                inc_blocks = []

                # incomplete blocks may be in the disk (serialized) or
                # memory (active) cache
                if my_key in self._disk_blocks:
                    disk_blocks = self._disk_blocks[my_key]
                    for serialized_block in disk_blocks:
                        # create new wrapped block from stored byte string
                        block = Block()
                        block.ParseFromString(serialized_block)
                        inc_blkw = BlockWrapper(block=block)
                        inc_blocks += [inc_blkw]
                    del self._disk_blocks[my_key]
                elif my_key in self._incomplete_blocks:
                    inc_blocks = self._incomplete_blocks[my_key]
                    del self._incomplete_blocks[my_key]

                # now process these children blocks of my_key
                LOGGER.debug("Processing %d incomplete blocks for parent %s",
                             len(inc_blocks), my_key)
                for inc_block in inc_blocks:
                    if self._complete_block(inc_block):
                        self._send_block(inc_block.block)
                        # use their ID to resolve if they have any children in
                        # the cache next
                        to_complete.append(inc_block.header_signature)
                    else:
                        LOGGER.warning("Failed to complete block %s",
                                       inc_block.header_signature)

    def _send_block(self, block):
        self._on_block_received(block.header_signature)

    def _wrap_block(self, block_data):
        block = Block()
        block.ParseFromString(block_data)
        blkw = BlockWrapper(block)
        return (block, blkw)

    def start(self):
        start_block = self.get_chain_head()
        blkw = None
        if start_block:
            blkw = BlockWrapper(start_block)
        LOGGER.info("Chain head is block: %s", blkw)
        if blkw:
            self._clean_cache(blkw)
            self._knit_cache()
            if blkw.header_signature in self._disk_blocks:
                LOGGER.info("Starting the disk catchup from block: %s", blkw)
                block_data = self._disk_blocks[blkw.header_signature][0]
                block = self._wrap_block(block_data)[0]
                self.add_block(block)
            else:
                LOGGER.info("No blocks to catch up from disk yet")
        else:
            self._knit_cache()
            if "0000000000000000" in self._disk_blocks:
                LOGGER.info("Starting the disk catchup from genesis")
                block_data = self._disk_blocks["0000000000000000"][0]
                block = self._wrap_block(block_data)[0]
                self.add_block(block)
            else:
                LOGGER.info("No chain head yet and disk blocks not cached to genesis")

    def set_get_chain_head(self, get_chain_head):
        self._get_chain_head = get_chain_head

    def set_on_block_received(self, on_block_received_func):
        self._on_block_received = on_block_received_func

    def set_on_batch_received(self, on_batch_received_func):
        self._on_batch_received = on_batch_received_func

    def add_block(self, block):
        with self.lock:
            blkw = BlockWrapper(block)
            block = self._complete_block(blkw)
            if block is not None:
                self._send_block(block.block)
                self._process_incomplete_blocks(block.header_signature)
            self._incomplete_blocks_length.set_value(
                len(self._incomplete_blocks))

    def add_batch(self, batch):
        with self.lock:
            if batch.header_signature in self._batch_cache:
                return
            if self._complete_batch(batch):
                self._batch_cache[batch.header_signature] = batch
                self._add_seen_txns(batch)
                self._on_batch_received(batch)
                self._process_incomplete_blocks(batch.header_signature)
                if batch.header_signature in self._requested:
                    del self._requested[batch.header_signature]
                # If there was a batch waiting on this transaction, process
                # that batch
                for txn in batch.transactions:
                    if txn.header_signature in self._incomplete_batches:
                        if txn.header_signature in self._requested:
                            del self._requested[txn.header_signature]
                        self._process_incomplete_batches(txn.header_signature)
            self._incomplete_batches_length.set_value(
                len(self._incomplete_batches))

    def get_chain_head(self):
        """Returns the block which is the current head of the chain.

        Returns:
            BlockWrapper: The head of the chain.
        """
        with self.lock:
            return self._get_chain_head()

    def get_block(self, block_id):
        with self.lock:
            try:
                return next(self._block_manager.get([block_id]))
            except StopIteration:
                return None

    def get_batch(self, batch_id):
        with self.lock:
            if batch_id in self._batch_cache:
                return self._batch_cache[batch_id]

            try:
                return self._get_committed_batch_by_id(batch_id)
            except ValueError:
                return None

    def get_batch_by_transaction(self, transaction_id):
        with self.lock:
            if transaction_id in self._seen_txns:
                batch_id = self._seen_txns[transaction_id]
                return self.get_batch(batch_id)

            try:
                return self._get_committed_batch_by_txn_id(
                    transaction_id)
            except ValueError:
                return None


class CompleterBatchListBroadcastHandler(Handler):
    def __init__(self, completer, gossip):
        self._completer = completer
        self._gossip = gossip

    def handle(self, connection_id, message_content):
        for batch in message_content.batches:
            if batch.trace:
                LOGGER.debug("TRACE %s: %s", batch.header_signature,
                             self.__class__.__name__)
            self._completer.add_batch(batch)
            self._gossip.broadcast_batch(batch)
        return HandlerResult(status=HandlerStatus.PASS)


class CompleterGossipHandler(Handler):
    def __init__(self, completer):
        self._completer = completer

    def handle(self, connection_id, message_content):
        obj, tag, _ = message_content

        if tag == network_pb2.GossipMessage.BLOCK:
            self._completer.add_block(obj)
        elif tag == network_pb2.GossipMessage.BATCH:
            self._completer.add_batch(obj)
        return HandlerResult(status=HandlerStatus.PASS)


class CompleterGossipBlockResponseHandler(Handler):
    def __init__(self, completer):
        self._completer = completer

    def handle(self, connection_id, message_content):
        block, _ = message_content
        self._completer.add_block(block)

        return HandlerResult(status=HandlerStatus.PASS)


class CompleterGossipBatchResponseHandler(Handler):
    def __init__(self, completer):
        self._completer = completer

    def handle(self, connection_id, message_content):
        batch, _ = message_content
        self._completer.add_batch(batch)

        return HandlerResult(status=HandlerStatus.PASS)
