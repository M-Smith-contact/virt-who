"""
Test for basic virt-who operations.

Copyright (C) 2014 Radek Novacek <rnovacek@redhat.com>

This program is free software; you can redistribute it and/or
modify it under the terms of the GNU General Public License
as published by the Free Software Foundation; either version 2
of the License, or (at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program; if not, write to the Free Software
Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.
"""

import sys
import os
from Queue import Empty, Queue
from mock import patch, Mock, sentinel, ANY, call

from base import TestBase

from stubs import StubDatastore

from virtwho import util
from virtwho.config import Config, ConfigManager
from virtwho.manager import ManagerThrottleError, ManagerFatalError
from virtwho.virt import (
    HostGuestAssociationReport, Hypervisor, Guest,
    DomainListReport, AbstractVirtReport)
from virtwho.parser import parseOptions, OptionError
from virtwho.executor import Executor, ReloadRequest
from virtwho.main import _main


class TestOptions(TestBase):
    NO_GENERAL_CONF = {'global': {}}

    def setUp(self):
        self.clearEnv()

    def clearEnv(self):
        for key in os.environ.keys():
            if key.startswith("VIRTWHO"):
                del os.environ[key]

    def setUpParseFile(self, parseFileMock):
        parseFileMock.return_value = TestOptions.NO_GENERAL_CONF

    @patch('virtwho.log.getLogger')
    @patch('virtwho.config.parseFile')
    def test_default_cmdline_options(self, parseFile, getLogger):
        self.setUpParseFile(parseFile)
        sys.argv = ["virtwho.py"]
        _, options = parseOptions()
        self.assertFalse(options.debug)
        self.assertFalse(options.background)
        self.assertFalse(options.oneshot)
        self.assertEqual(options.interval, 3600)
        self.assertEqual(options.smType, 'sam')
        self.assertEqual(options.virtType, None)
        self.assertEqual(options.reporter_id, util.generateReporterId())

    @patch('virtwho.log.getLogger')
    @patch('virtwho.config.parseFile')
    def test_minimum_interval_options(self, parseFile, getLogger):
        self.setUpParseFile(parseFile)
        sys.argv = ["virtwho.py", "--interval=5"]
        _, options = parseOptions()
        self.assertEqual(options.interval, 60)

        sys.argv = ["virtwho.py"]
        os.environ["VIRTWHO_INTERVAL"] = '1'

        _, options = parseOptions()
        self.assertEqual(options.interval, 60)

        self.clearEnv()
        bad_conf = {'global': {'interval': 1}}
        parseFile.return_value = bad_conf

        _, options = parseOptions()
        self.assertEqual(options.interval, 60)

    @patch('virtwho.log.getLogger')
    @patch('virtwho.config.parseFile')
    def test_options_hierarchy_for_reporter_id(self, parseFile, getLogger):
        # Set the value in all three possible locations
        # Mock /etc/virt-who.conf file
        global_conf_dict = {
            'global': {
                'reporter_id': "/etc/virt-who.conf"
            }
        }
        parseFile.return_value = global_conf_dict
        # cli option
        sys.argv = ["virtwho.py", "--reporter-id=cli"]
        # environment var
        os.environ["VIRTWHO_REPORTER_ID"] = "env"
        _, options = parseOptions()
        # cli option should beat environment vars and virt-who.conf
        self.assertEqual(options.reporter_id, "cli")

        sys.argv = ["virtwho.py"]

        _, options = parseOptions()
        self.assertEqual(options.reporter_id, "env")

        self.clearEnv()

        _, options = parseOptions()
        self.assertEqual(options.reporter_id, "/etc/virt-who.conf")

        parseFile.return_value = {'global': {}}

        _, options = parseOptions()
        self.assertEqual(options.reporter_id, util.generateReporterId())

    @patch('virtwho.log.getLogger')
    @patch('virtwho.config.parseFile')
    def test_options_debug(self, parseFile, getLogger):
        self.setUpParseFile(parseFile)
        sys.argv = ["virtwho.py", "-d"]
        _, options = parseOptions()
        self.assertTrue(options.debug)

        sys.argv = ["virtwho.py"]
        os.environ["VIRTWHO_DEBUG"] = "1"
        _, options = parseOptions()
        self.assertTrue(options.debug)

    @patch('virtwho.log.getLogger')
    @patch('virtwho.config.parseFile')
    def test_options_virt(self, parseFile, getLogger):
        self.setUpParseFile(parseFile)
        for virt in ['esx', 'hyperv', 'rhevm']:
            self.clearEnv()
            sys.argv = ["virtwho.py", "--%s" % virt, "--%s-owner=owner" % virt,
                        "--%s-env=env" % virt, "--%s-server=localhost" % virt,
                        "--%s-username=username" % virt,
                        "--%s-password=password" % virt]
            _, options = parseOptions()
            self.assertEqual(options.virtType, virt)
            self.assertEqual(options.owner, 'owner')
            self.assertEqual(options.env, 'env')
            self.assertEqual(options.server, 'localhost')
            self.assertEqual(options.username, 'username')
            self.assertEqual(options.password, 'password')

            sys.argv = ["virtwho.py"]
            virt_up = virt.upper()
            os.environ["VIRTWHO_%s" % virt_up] = "1"
            os.environ["VIRTWHO_%s_OWNER" % virt_up] = "xowner"
            os.environ["VIRTWHO_%s_ENV" % virt_up] = "xenv"
            os.environ["VIRTWHO_%s_SERVER" % virt_up] = "xlocalhost"
            os.environ["VIRTWHO_%s_USERNAME" % virt_up] = "xusername"
            os.environ["VIRTWHO_%s_PASSWORD" % virt_up] = "xpassword"
            _, options = parseOptions()
            self.assertEqual(options.virtType, virt)
            self.assertEqual(options.owner, 'xowner')
            self.assertEqual(options.env, 'xenv')
            self.assertEqual(options.server, 'xlocalhost')
            self.assertEqual(options.username, 'xusername')
            self.assertEqual(options.password, 'xpassword')

    @patch('virtwho.log.getLogger')
    @patch('virtwho.config.parseFile')
    def test_options_virt_satellite(self, parseFile, getLogger):
        self.setUpParseFile(parseFile)
        for virt in ['esx', 'hyperv', 'rhevm']:
            self.clearEnv()
            sys.argv = ["virtwho.py",
                        "--satellite",
                        "--satellite-server=localhost",
                        "--satellite-username=username",
                        "--satellite-password=password",
                        "--%s" % virt,
                        "--%s-server=localhost" % virt,
                        "--%s-username=username" % virt,
                        "--%s-password=password" % virt]
            _, options = parseOptions()
            self.assertEqual(options.virtType, virt)
            self.assertEqual(options.owner, '')
            self.assertEqual(options.env, '')
            self.assertEqual(options.server, 'localhost')
            self.assertEqual(options.username, 'username')
            self.assertEqual(options.password, 'password')

            sys.argv = ["virtwho.py"]
            virt_up = virt.upper()
            os.environ["VIRTWHO_SATELLITE"] = "1"
            os.environ["VIRTWHO_SATELLITE_SERVER"] = "xlocalhost"
            os.environ["VIRTWHO_SATELLITE_USERNAME"] = "xusername"
            os.environ["VIRTWHO_SATELLITE_PASSWORD"] = "xpassword"
            os.environ["VIRTWHO_%s" % virt_up] = "1"
            os.environ["VIRTWHO_%s_SERVER" % virt_up] = "xlocalhost"
            os.environ["VIRTWHO_%s_USERNAME" % virt_up] = "xusername"
            os.environ["VIRTWHO_%s_PASSWORD" % virt_up] = "xpassword"
            _, options = parseOptions()
            self.assertEqual(options.virtType, virt)
            self.assertEqual(options.owner, '')
            self.assertEqual(options.env, '')
            self.assertEqual(options.server, 'xlocalhost')
            self.assertEqual(options.username, 'xusername')
            self.assertEqual(options.password, 'xpassword')

    @patch('virtwho.log.getLogger')
    @patch('virtwho.config.parseFile')
    def test_missing_option(self, parseFile, getLogger):
        self.setUpParseFile(parseFile)
        for smType in ['satellite', 'sam']:
            for virt in ['libvirt', 'vdsm', 'esx', 'hyperv', 'rhevm']:
                for missing in ['server', 'username', 'password', 'env', 'owner']:
                    self.clearEnv()
                    sys.argv = ["virtwho.py", "--%s" % virt]
                    if virt in ['libvirt', 'esx', 'hyperv', 'rhevm']:
                        if missing != 'server':
                            sys.argv.append("--%s-server=localhost" % virt)
                        if missing != 'username':
                            sys.argv.append("--%s-username=username" % virt)
                        if missing != 'password':
                            sys.argv.append("--%s-password=password" % virt)
                        if missing != 'env':
                            sys.argv.append("--%s-env=env" % virt)
                        if missing != 'owner':
                            sys.argv.append("--%s-owner=owner" % virt)

                    if virt not in ('libvirt', 'vdsm') and missing != 'password':
                        if smType == 'satellite' and missing in ['env', 'owner']:
                            continue
                        self.assertRaises(OptionError, parseOptions)

    # @patch('virtwho.log.getLogger')
    # @patch('virtwho.virt.Virt.from_config')
    # @patch('virtwho.manager.Manager.fromOptions')
    # @patch('virtwho.config.parseFile')
    # def test_sending_guests(self, parseFile, fromOptions, from_config, getLogger):
    #     # TODO: Needs to be rewritten
    #     self.setUpParseFile(parseFile)
    #     options = Mock()
    #     options.oneshot = True
    #     options.interval = 0
    #     options.print_ = False
    #     fake_virt = Mock()
    #     fake_virt.CONFIG_TYPE = 'esx'
    #     test_hypervisor = Hypervisor('test', guestIds=[Guest('guest1', fake_virt, 1)])
    #     association = {'hypervisors': [test_hypervisor]}
    #     options.log_dir = ''
    #     options.log_file = ''
    #     getLogger.return_value = sentinel.logger
    #
    #     virtwho = Executor(self.logger, options, config_dir="/nonexistant")
    #     config = Config("test", "esx", server="localhost", username="username",
    #                     password="password", owner="owner", env="env")
    #     expected_virt = Mock()
    #     expected_virt.config = config
    #     from_config.return_value = expected_virt
    #     virtwho.configManager.addConfig(config)
    #     test_queue = Queue()
    #     virtwho.queue = test_queue
    #     virtwho.queue.put(HostGuestAssociationReport(config, association))
    #     virtwho.run()
    #
    #     from_config.assert_called_with(sentinel.logger, config, test_queue,
    #                                    terminate_event=virtwho.terminate_event,
    #                                    interval=options.interval,
    #                                    oneshot=options.oneshot)
    #     self.assertTrue(from_config.return_value.start.called)
    #     fromOptions.assert_called_with(self.logger, options, ANY)


class TestExecutor(TestBase):

    def test_terminated(self):
        pass

    def test_stop_threads(self):
        pass

    def test_terminate(self):
        pass

    def test_reload(self):
        pass

    @patch.object(Executor, 'terminate_threads')
    @patch('virtwho.executor.time')
    def test_wait_on_threads(self, mock_time, mock_terminate_threads):
        """
        Tests that, given no kwargs, the wait_on_threads method will wait until
        all threads is_terminated method returns True.
        Please note that a possible consequence of something going wrong in
        the wait on threads method (with no kwargs) could cause this test to
        never quit.
        """
        # Create a few mock threads
        # The both will return False the first time is_terminated is called
        # Only the second mock thread will wait not return True until the
        # third call of is_terminated
        mock_thread1 = Mock()
        mock_thread1.is_terminated = Mock(side_effect=[False, True])
        mock_thread2 = Mock()
        mock_thread2.is_terminated = Mock(side_effect=[False, False, True])

        threads = [mock_thread1, mock_thread2]

        mock_time.sleep = Mock()
        Executor.wait_on_threads(threads)
        mock_time.sleep.assert_has_calls([
            call(1),
            call(1),
        ])
        mock_terminate_threads.assert_not_called()

    def test_terminate_threads(self):
        threads = [Mock(), Mock()]
        Executor.terminate_threads(threads)
        for mock_thread in threads:
            mock_thread.stop.assert_called()
            mock_thread.join.assert_called()

    def test_run_oneshot(self):
        pass

    def test_run(self):
        pass


class TestReload(TestBase):
    def mock_virtwho(self):
        options = Mock()
        options.interval = 6
        options.oneshot = False
        options.print_ = False
        virtwho = Executor(Mock(), options, config_dir="/nonexistant")
        config = Config("env/cmdline", 'libvirt')
        virtwho.configManager.addConfig(config)
        virtwho.datastore = {}
        virtwho._create_destinations = Mock()
        virtwho._create_virt_backends = Mock()
        return virtwho

    def assertStartStop(self, from_config):
        ''' Make sure that Virt was started and stopped. '''
        self.assertTrue(from_config.return_value.start.called)
        self.assertTrue(from_config.return_value.stop.called)

    @patch('virtwho.log.getLogger')
    @patch('virtwho.virt.Virt.from_config')
    def test_start_unregistered(self, from_config, getLogger):
        virtwho = self.mock_virtwho()
        virtwho.queue.get.side_effect = [DomainListReport(virtwho.configManager.configs[0], []), Empty, 'reload']
        virtwho.send.side_effect = ManagerFatalError
        # When not registered, it should throw ReloadRequest
        self.assertRaises(ReloadRequest, _main, virtwho)
        # queue.get should be called 3 times: report, nonblocking reading
        # of remaining reports and after ManagerFatalError wait indefinately
        self.assertEqual(virtwho.queue.get.call_count, 3)
        # It should wait blocking for the reload
        virtwho.queue.get.assert_has_calls([call(block=True)])
        self.assertStartStop(from_config)

    # @patch('virtwho.log.getLogger')
    # @patch('virtwho.virt.Virt.from_config')
    # def test_exit_after_unregister_one_dest(self, from_config, getLogger):
    #     """
    #     """
    #     virtwho = self.mock_virtwho()
    #     report = DomainListReport(virtwho.configManager.configs[0], [])
    #     mock_dest = Mock()
    #     mock_dest.is_terminated = Mock(side_effect=[False, True])
    #
    #     # _main should exit normally
    #     _main(virtwho)
    #     self.assertStartStop(from_config)

    @patch('virtwho.log.getLogger')
    @patch('virtwho.virt.Virt.from_config')
    def test_reload_after_unregister(self, from_config, getLogger):
        virtwho = self.mock_virtwho()
        report = DomainListReport(virtwho.configManager.configs[0], [])
        # Send two reports and then 'reload'
        virtwho.queue.get.side_effect = [report, Empty, report, Empty, 'reload']
        # First report will be successful, second one will throw ManagerFatalError
        virtwho.send.side_effect = [True, ManagerFatalError]
        # _main should throw ReloadRequest
        self.assertRaises(ReloadRequest, _main, virtwho)
        self.assertStartStop(from_config)

    @patch('virtwho.log.getLogger')
    @patch('virtwho.virt.Virt.from_config')
    def test_reload_after_register(self, from_config, getLogger):
        virtwho = self.mock_virtwho()
        report = DomainListReport(virtwho.configManager.configs[0], [])
        # Send report and then 'reload'
        virtwho.queue.get.side_effect = [report, Empty, 'reload']
        # First report will be successful, second one will throw ManagerFatalError
        virtwho.send.side_effect = [ManagerFatalError, True]
        # _main should throw ReloadRequest
        self.assertRaises(ReloadRequest, _main, virtwho)

        self.assertEqual(virtwho.queue.get.call_count, 3)
        # It should wait blocking for the reload
        virtwho.queue.get.assert_has_calls([call(block=True)])
        self.assertStartStop(from_config)
