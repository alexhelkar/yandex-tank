""" Core module to calculate aggregate data """
import logging
import json
import queue as q
from pkg_resources import resource_string

from ...core.interfaces import AbstractPlugin
from ...core.interfaces import AggregateResultListener
from ...core.util import Drain
from ...core.exceptions import PluginImplementationError
from .aggregator import Aggregator, DataPoller
from .chopper import TimeChopper

logger = logging.getLogger(__name__)


class LoggingListener(AggregateResultListener):
    """ Log aggregated results """

    def on_aggregated_data(self, data, stats):
        logger.info("Got aggregated sample:\n%s", json.dumps(data, indent=2))
        logger.info("Stats:\n%s", json.dumps(stats, indent=2))


class AggregatorPlugin(AbstractPlugin):
    """
    Plugin that manages aggregation and stats collection
    """

    SECTION = 'aggregator'

    @staticmethod
    def get_key():
        return __file__

    def __init__(self, core):
        AbstractPlugin.__init__(self, core)
        self.listeners = []  # [LoggingListener()]
        self.reader = None
        self.stats_reader = None
        self.results = q.Queue()
        self.stats = q.Queue()
        self.verbose_histogram = False
        self.data_cache = {}
        self.stat_cache = {}

    def get_available_options(self):
        return ["verbose_histogram"]

    def configure(self):
        self.aggregator_config = json.loads(resource_string(
            __name__, 'config/phout.json'))
        verbose_histogram_option = self.get_option("verbose_histogram", "0")
        self.verbose_histogram = (
            verbose_histogram_option.lower() == "true") or (
                verbose_histogram_option.lower() == "1")
        if self.verbose_histogram:
            logger.info("using verbose histogram")

    def start_test(self):
        if self.reader and self.stats_reader:
            pipeline = Aggregator(
                TimeChopper(
                    DataPoller(source=self.reader,
                               poll_period=1),
                    cache_size=3),
                self.aggregator_config,
                self.verbose_histogram)
            self.drain = Drain(pipeline, self.results)
            self.drain.start()
            self.stats_drain = Drain(
                DataPoller(source=self.stats_reader,
                           poll_period=1),
                self.stats)
            self.stats_drain.start()
        else:
            raise PluginImplementationError(
                "Generator must pass a Reader and a StatsReader"
                " to Aggregator before starting test")

    def _collect_data(self):
        """
        Collect data, cache it and send to listeners
        """
        data = []
        for _ in range(self.results.qsize()):
            try:
                data.append(self.results.get_nowait())
            except q.Empty:
                break
        stats = []
        for _ in range(self.stats.qsize()):
            try:
                stats += self.stats.get_nowait()
            except q.Empty:
                break
        logger.debug("Data timestamps:\n%s" % [d.get('ts') for d in data])
        logger.debug("Stats timestamps:\n%s" % [d.get('ts') for d in stats])
        logger.debug("Data cache timestamps:\n%s" % self.data_cache.keys())
        logger.debug("Stats cache timestamps:\n%s" % self.stat_cache.keys())
        for item in data:
            ts = item['ts']
            if ts in self.stat_cache:
                # send items
                data_item = item
                stat_item = self.stat_cache.pop(ts)
                self.__notify_listeners(data_item, stat_item)
            else:
                self.data_cache[ts] = item
        for item in stats:
            ts = item['ts']
            if ts in self.data_cache:
                # send items
                data_item = self.data_cache.pop(ts)
                stat_item = item
                self.__notify_listeners(data_item, stat_item)
            else:
                self.stat_cache[ts] = item

    def is_test_finished(self):
        self._collect_data()
        return -1

    def end_test(self, retcode):
        if self.reader:
            self.reader.close()
        if self.drain:
            self.drain.wait()
        if self.stats_reader:
            self.stats_reader.close()
        if self.stats_drain:
            self.stats_drain.wait()
        self._collect_data()
        return retcode

    def add_result_listener(self, listener):
        self.listeners.append(listener)

    def __notify_listeners(self, data, stats):
        """ notify all listeners about aggregate data and stats """
        for listener in self.listeners:
            listener.on_aggregated_data(data, stats)
