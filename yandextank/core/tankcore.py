""" The central part of the tool: Core """
import ConfigParser
import datetime
import logging
import os
import shutil
import tempfile
import time
import traceback
import fnmatch
import importlib as il
import json
import uuid
from ConfigParser import NoSectionError

from ..core.resource import manager as resource
from ..core.util import update_status, execute, pid_exists


class TankCore(object):
    """
    JMeter + dstat inspired :)
    """
    SECTION = 'tank'
    PLUGIN_PREFIX = 'plugin_'
    PID_OPTION = 'pid'
    UUID_OPTION = 'uuid'
    LOCK_DIR = '/var/lock'

    def __init__(self):
        self.log = logging.getLogger(__name__)
        self.config = ConfigManager()
        self.status = {}
        self.plugins = []
        self.artifacts_dir = None
        self.artifact_files = {}
        self.artifacts_base_dir = '.'
        self.manual_start = False
        self.scheduled_start = None
        self.interrupted = False
        self.lock_file = None
        self.flush_config_to = None
        self.lock_dir = None
        self.taskset_path = None
        self.taskset_affinity = None
        self.uuid = str(uuid.uuid4())
        self.set_option(self.SECTION, self.UUID_OPTION, self.uuid)
        self.set_option(self.SECTION, self.PID_OPTION, str(os.getpid()))

    def get_uuid(self):
        return self.uuid

    def get_available_options(self):
        return ["artifacts_base_dir", "artifacts_dir", "flush_config_to",
                "taskset_path", "affinity"]

    def load_configs(self, configs):
        """ Tells core to load configs set into options storage """
        self.log.info("Loading configs...")
        self.config.load_files(configs)
        dotted_options = []
        for option, value in self.config.get_options(self.SECTION):
            if '.' in option:
                dotted_options += [option + '=' + value]
        self.apply_shorthand_options(dotted_options, self.SECTION)
        self.config.flush()
        self.add_artifact_file(self.config.file)
        self.set_option(self.SECTION, self.PID_OPTION, str(os.getpid()))
        self.flush_config_to = self.get_option(self.SECTION, "flush_config_to",
                                               "")
        if self.flush_config_to:
            self.config.flush(self.flush_config_to)

    def load_plugins(self):
        """
        Tells core to take plugin options and instantiate plugin classes
        """
        self.log.info("Loading plugins...")

        base_dir = self.get_option(self.SECTION, "artifacts_base_dir",
                                   self.artifacts_base_dir)
        self.artifacts_base_dir = os.path.expanduser(base_dir)
        self.artifacts_dir = self.get_option(self.SECTION, "artifacts_dir", "")
        self.taskset_path = self.get_option(self.SECTION, 'taskset_path',
                                            'taskset')
        self.taskset_affinity = self.get_option(self.SECTION, 'affinity', '')

        options = self.config.get_options(self.SECTION, self.PLUGIN_PREFIX)
        for (plugin_name, plugin_path) in options:
            if not plugin_path:
                self.log.debug("Seems the plugin '%s' was disabled",
                               plugin_name)
                continue
            self.log.debug("Loading plugin %s from %s", plugin_name,
                           plugin_path)
            if '/' in plugin_path:
                self.log.warning("Deprecated plugin path format: %s\n"
                                 "Should be in pythonic format. Example:\n"
                                 "    plugin_jmeter=yandextank.plugins.JMeter",
                                 plugin_path)
                if plugin_path.startswith("Tank/Plugins/"):
                    plugin_path = "yandextank.plugins." + \
                        plugin_path.split('/')[-1].split('.')[0]
                    self.log.warning("Converted plugin path to %s",
                                     plugin_path)
                else:
                    raise ValueError(
                        "Couldn't convert plugin path to new format:\n    %s" %
                        plugin_path)
            plugin = il.import_module(plugin_path)
            try:
                instance = getattr(plugin, 'Plugin')(self)
            except:
                self.log.warning("Deprecated plugin classname: %s. Should be 'Plugin'", plugin)
                instance = getattr(plugin, plugin_path.split('.')[-1] + 'Plugin')(self)

            self.plugins.append(instance)

        self.log.debug("Plugin instances: %s", self.plugins)

    def plugins_configure(self):
        """        Call configure() on all plugins        """
        self.publish("core", "stage", "configure")
        if not os.path.exists(self.artifacts_base_dir):
            os.makedirs(self.artifacts_base_dir)
            os.chmod(self.artifacts_base_dir, 0755)

        self.log.info("Configuring plugins...")
        if self.taskset_affinity != '':
            self.taskset(os.getpid(), self.taskset_path, self.taskset_affinity)
        for plugin in self.plugins:
            self.log.debug("Configuring %s", plugin)
            plugin.configure()
            self.config.flush()
        if self.flush_config_to:
            self.config.flush(self.flush_config_to)

    def plugins_prepare_test(self):
        """ Call prepare_test() on all plugins        """
        self.log.info("Preparing test...")
        self.publish("core", "stage", "prepare")
        for plugin in self.plugins:
            self.log.debug("Preparing %s", plugin)
            plugin.prepare_test()
        if self.flush_config_to:
            self.config.flush(self.flush_config_to)

    def plugins_start_test(self):
        """        Call start_test() on all plugins        """
        self.log.info("Starting test...")
        self.publish("core", "stage", "start")
        for plugin in self.plugins:
            self.log.debug("Starting %s", plugin)
            plugin.start_test()
        if self.flush_config_to:
            self.config.flush(self.flush_config_to)

    def wait_for_finish(self):
        """
        Call is_test_finished() on all plugins 'till one of them initiates exit
        """

        self.log.info("Waiting for test to finish...")
        self.publish("core", "stage", "shoot")
        if not self.plugins:
            raise RuntimeError("It's strange: we have no plugins loaded...")

        while not self.interrupted:
            begin_time = time.time()
            for plugin in self.plugins:
                self.log.debug("Polling %s", plugin)
                retcode = plugin.is_test_finished()
                if retcode >= 0:
                    return retcode
            end_time = time.time()
            diff = end_time - begin_time
            self.log.debug("Polling took %s", diff)
            self.log.debug("Tank status:\n%s",
                           json.dumps(self.status,
                                      indent=2))
            # screen refresh every 0.5 s
            if diff < 0.5:
                time.sleep(0.5 - diff)
        return 1

    def plugins_end_test(self, retcode):
        """        Call end_test() on all plugins        """
        self.log.info("Finishing test...")
        self.publish("core", "stage", "end")

        for plugin in self.plugins:
            self.log.debug("Finalize %s", plugin)
            try:
                self.log.debug("RC before: %s", retcode)
                plugin.end_test(retcode)
                self.log.debug("RC after: %s", retcode)
            except Exception, ex:
                self.log.error("Failed finishing plugin %s: %s", plugin, ex)
                self.log.debug("Failed finishing plugin: %s",
                               traceback.format_exc(ex))
                if not retcode:
                    retcode = 1

        if self.flush_config_to:
            self.config.flush(self.flush_config_to)
        return retcode

    def plugins_post_process(self, retcode):
        """
        Call post_process() on all plugins
        """
        self.log.info("Post-processing test...")
        self.publish("core", "stage", "post_process")

        for plugin in self.plugins:
            self.log.debug("Post-process %s", plugin)
            try:
                self.log.debug("RC before: %s", retcode)
                retcode = plugin.post_process(retcode)
                self.log.debug("RC after: %s", retcode)
            except Exception, ex:
                self.log.error("Failed post-processing plugin %s: %s", plugin,
                               ex)
                self.log.debug("Failed post-processing plugin: %s",
                               traceback.format_exc(ex))
                if not retcode:
                    retcode = 1

        if self.flush_config_to:
            self.config.flush(self.flush_config_to)

        self.__collect_artifacts()

        return retcode

    def taskset(self, pid, path, affinity):
        if affinity != '':
            args = "%s -pc %s %s" % (path, affinity, pid)
            retcode, stdout, stderr = execute(args,
                                              shell=True,
                                              poll_period=0.1,
                                              catch_out=True)
            self.log.debug('taskset stdout: %s', stdout)
            if retcode != 0:
                raise KeyError(stderr)
            else:
                self.log.info("Enabled taskset for pid %s with affinity %s",
                              str(pid), affinity)

    def __collect_artifacts(self):
        self.log.debug("Collecting artifacts")
        if not self.artifacts_dir:
            date_str = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S.")
            self.artifacts_dir = tempfile.mkdtemp("", date_str,
                                                  self.artifacts_base_dir)
        else:
            self.artifacts_dir = os.path.expanduser(self.artifacts_dir)

        if not os.path.isdir(self.artifacts_dir):
            os.makedirs(self.artifacts_dir)

        os.chmod(self.artifacts_dir, 0755)

        self.log.info("Artifacts dir: %s", self.artifacts_dir)
        for filename, keep in self.artifact_files.items():
            try:
                self.__collect_file(filename, keep)
            except Exception, ex:
                self.log.warn("Failed to collect file %s: %s", filename, ex)

    def get_option(self, section, option, default=None):
        """
        Get an option from option storage
        """
        if not self.config.config.has_section(section):
            self.log.debug("No section '%s', adding", section)
            self.config.config.add_section(section)

        try:
            value = self.config.config.get(section, option).strip()
        except ConfigParser.NoOptionError as ex:
            if default is not None:
                default = str(default)
                self.config.config.set(section, option, default)
                self.config.flush()
                value = default.strip()
            else:
                self.log.warn(
                    "Mandatory option %s was not found in section %s", option,
                    section)
                raise ex

        if len(value) > 1 and value[0] == '`' and value[-1] == '`':
            self.log.debug("Expanding shell option %s", value)
            retcode, stdout, stderr = execute(value[1:-1], True, 0.1, True)
            if retcode or stderr:
                raise ValueError("Error expanding option %s, RC: %s" %
                                 (value, retcode))
            value = stdout.strip()

        return value

    def set_option(self, section, option, value):
        """
        Set an option in storage
        """
        if not self.config.config.has_section(section):
            self.config.config.add_section(section)
        self.config.config.set(section, option, value)
        self.config.flush()

    def get_plugin_of_type(self, plugin_class):
        """
        Retrieve a plugin of desired class, KeyError raised otherwise
        """
        self.log.debug("Searching for plugin: %s", plugin_class)
        matches = [plugin
                   for plugin in self.plugins
                   if isinstance(plugin, plugin_class)]
        if len(matches) > 0:
            if len(matches) > 1:
                self.log.debug(
                    "More then one plugin of type %s found. Using first one.",
                    plugin_class)
            return matches[-1]
        else:
            raise KeyError("Requested plugin type not found: %s" %
                           plugin_class)

    def __collect_file(self, filename, keep_original=False):
        """
        Move or copy single file to artifacts dir
        """
        if not self.artifacts_dir:
            self.log.warning("No artifacts dir configured")
            return

        dest = self.artifacts_dir + '/' + os.path.basename(filename)
        self.log.debug("Collecting file: %s to %s", filename, dest)
        if not filename or not os.path.exists(filename):
            self.log.warning("File not found to collect: %s", filename)
            return

        if os.path.exists(dest):
            # FIXME: 3 find a way to store artifacts anyway
            self.log.warning("File already exists: %s", dest)
            return

        if keep_original:
            shutil.copy(filename, self.artifacts_dir)
        else:
            shutil.move(filename, self.artifacts_dir)

        os.chmod(dest, 0644)

    def add_artifact_file(self, filename, keep_original=False):
        """
        Add file to be stored as result artifact on post-process phase
        """
        if filename:
            self.log.debug("Adding artifact file to collect (keep=%s): %s",
                           keep_original, filename)
            self.artifact_files[filename] = keep_original

    def apply_shorthand_options(self, options, default_section='DEFAULT'):
        for option_str in options:
            try:
                section = option_str[:option_str.index('.')]
                option = option_str[
                    option_str.index('.') + 1:option_str.index('=')]
            except ValueError:
                section = default_section
                option = option_str[:option_str.index('=')]
            value = option_str[option_str.index('=') + 1:]
            self.log.debug("Override option: %s => [%s] %s=%s", option_str,
                           section, option, value)
            self.set_option(section, option, value)

    def get_lock_dir(self):
        if not self.lock_dir:
            self.lock_dir = self.get_option(self.SECTION, "lock_dir",
                                            self.LOCK_DIR)

        return os.path.expanduser(self.lock_dir)

    def get_lock(self, force=False):
        if not force and self.__there_is_locks():
            raise RuntimeError("There is lock files")

        fh, self.lock_file = tempfile.mkstemp('.lock', 'lunapark_',
                                              self.get_lock_dir())
        os.close(fh)
        os.chmod(self.lock_file, 0644)
        self.config.file = self.lock_file
        self.config.flush()

    def release_lock(self):
        self.config.file = None
        if self.lock_file and os.path.exists(self.lock_file):
            self.log.debug("Releasing lock: %s", self.lock_file)
            os.remove(self.lock_file)

    def __there_is_locks(self):
        retcode = False
        lock_dir = self.get_lock_dir()
        for filename in os.listdir(lock_dir):
            if fnmatch.fnmatch(filename, 'lunapark_*.lock'):
                full_name = os.path.join(lock_dir, filename)
                self.log.warn("Lock file present: %s", full_name)

                try:
                    info = ConfigParser.ConfigParser()
                    info.read(full_name)
                    pid = info.get(TankCore.SECTION, self.PID_OPTION)
                    if not pid_exists(int(pid)):
                        self.log.debug("Lock PID %s not exists, ignoring and "
                                       "trying to remove", pid)
                        try:
                            os.remove(full_name)
                        except Exception, exc:
                            self.log.debug("Failed to delete lock %s: %s",
                                           full_name, exc)
                    else:
                        retcode = True
                except Exception, exc:
                    self.log.warn("Failed to load info from lock %s: %s",
                                  full_name, exc)
                    retcode = True
        return retcode

    def mkstemp(self, suffix, prefix, directory=None):
        """
        Generate temp file name in artifacts base dir
        and close temp file handle
        """
        if not directory:
            directory = self.artifacts_base_dir
        fd, fname = tempfile.mkstemp(suffix, prefix, directory)
        os.close(fd)
        os.chmod(fname, 0644)  # FIXME: chmod to parent dir's mode?
        return fname

    def publish(self, publisher, key, value):
        update_status(self.status, [publisher] + key.split('.'), value)

    def close(self):
        """
        Call close() for all plugins
        """
        self.log.info("Close allocated resources...")

        for plugin in self.plugins:
            self.log.debug("Close %s", plugin)
            try:
                plugin.close()
            except Exception as ex:
                self.log.error("Failed closing plugin %s: %s", plugin, ex)
                self.log.debug("Failed closing plugin: %s",
                               traceback.format_exc(ex))


class ConfigManager(object):
    """ Option storage class """

    def __init__(self):
        self.file = None
        self.log = logging.getLogger(__name__)
        self.config = ConfigParser.ConfigParser()

    def load_files(self, configs):
        """         Read configs set into storage        """
        self.log.debug("Reading configs: %s", configs)
        config_filenames = [resource.resource_filename(config)
                            for config in configs]
        try:
            self.config.read(config_filenames)
        except Exception as ex:
            self.log.error("Can't load configs: %s", ex)
            raise ex

    def flush(self, filename=None):
        """        Flush current stat to file        """
        if not filename:
            filename = self.file

        if filename:
            with open(filename, 'wb') as handle:
                self.config.write(handle)

    def get_options(self, section, prefix=''):
        """ Get options list with requested prefix """
        res = []
        try:
            for option in self.config.options(section):
                if not prefix or option.find(prefix) == 0:
                    res += [(option[len(prefix):],
                             self.config.get(section, option))]
        except NoSectionError, ex:
            self.log.warning("No section: %s", ex)

        self.log.debug("Section: [%s] prefix: '%s' options:\n%s", section,
                       prefix, res)
        return res

    def find_sections(self, prefix):
        """ return sections with specified prefix """
        res = []
        for section in self.config.sections():
            if section.startswith(prefix):
                res.append(section)
        return res
