#!/usr/bin/env python

# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
#
# See LICENSE for more details.
#
# Copyright (c) 2016 ScyllaDB

import os
import re
import time
import tempfile
import itertools
import string
import yaml
from cassandra import AlreadyExists, InvalidRequest

from sdcm.tester import ClusterTester
from sdcm.utils.decorators import retrying


class EmptyResultException(Exception):
    pass


class BootstrapTest(ClusterTester):
    """
    The idea of this class is to test bootstrap time (if there is a reduction in bootstrap time after
    scylladb/scylla#5109 was merged.
    """

    def add_one_node_and_wait(self, check_bootstrap_time=True):
        start_bootstrap_pattern = "storage_service - JOINING: Starting to bootstrap..."
        end_bootstrap_pattern = "storage_service - NORMAL: node is now in normal status"
        new_nodes = self.db_cluster.add_nodes(count=1, enable_auto_bootstrap=True)
        self.monitors.reconfigure_scylla_monitoring()
        self.db_cluster.wait_for_init(node_list=new_nodes)
        if check_bootstrap_time:
            start = self.search_patter_in_log(node=new_nodes[0], pattern=start_bootstrap_pattern)
            end = self.search_patter_in_log(node=new_nodes[0], pattern=end_bootstrap_pattern)
            self.log.info(f'start of bootstrap time = {int(start)}')
            self.log.info(f'end of bootstrap time = {int(end)}')
            self.log.info(f'took {int(end) - int(start)} seconds to bootstrap node {new_nodes[0].name}')

    @retrying(n=100, sleep_time=10, allowed_exceptions=EmptyResultException)
    def search_patter_in_log(self, node, pattern):  # pylint: disable=no-self-use
        # res = node.remoter.run(f'sudo grep -q "{pattern}"', ignore_status=True)
        res = node.remoter.run(f'date -d "`sudo grep \"{pattern}\" /var/log/messages | head -1 | '
                               f'awk \'{{print $1 " " $2 " " $3}}\'`" +"%s"', ignore_status=True)
        if res.return_code != 0:
            raise EmptyResultException
        return res.stdout

    def run_load(self):
        pass

    def _pre_create_templated_user_schema(self, batch_start=None, batch_end=None):
        # pylint: disable=too-many-locals
        user_profile_table_count = self.params.get(  # pylint: disable=invalid-name
            'user_profile_table_count', default=0)
        cs_user_profiles = self.params.get('cs_user_profiles', default=[])
        # read user-profile
        for profile_file in cs_user_profiles:
            profile_yaml = yaml.load(open(profile_file), Loader=yaml.SafeLoader)
            keyspace_definition = profile_yaml['keyspace_definition']
            keyspace_name = profile_yaml['keyspace']
            table_template = string.Template(profile_yaml['table_definition'])

            with self.cql_connection_patient(node=self.db_cluster.nodes[0]) as session:
                try:
                    session.execute(keyspace_definition)
                except AlreadyExists:
                    self.log.debug("keyspace [{}] exists".format(keyspace_name))

                if batch_start is not None and batch_end is not None:
                    table_range = range(batch_start, batch_end)
                else:
                    table_range = range(user_profile_table_count)
                self.log.debug('Pre Creating Schema for c-s with {} user tables'.format(user_profile_table_count))
                for i in table_range:
                    table_name = 'table{}'.format(i)
                    query = table_template.substitute(table_name=table_name)
                    try:
                        session.execute(query)
                    except AlreadyExists:
                        self.log.debug('table [{}] exists'.format(table_name))
                    self.log.debug('{} Created'.format(table_name))

                    for definition in profile_yaml.get('extra_definitions', []):
                        query = string.Template(definition).substitute(table_name=table_name)
                        try:
                            session.execute(query)
                        except (AlreadyExists, InvalidRequest) as exc:
                            self.log.debug('extra definition for [{}] exists [{}]'.format(table_name, str(exc)))

    def create_templated_user_stress_params(self, idx, cs_profile):  # pylint: disable=invalid-name
        # pylint: disable=too-many-locals
        params_list = []
        cs_duration = self.params.get('cs_duration', default='50m')

        with open(cs_profile) as pconf:
            cont = pconf.readlines()
            pconf.seek(0)
            template = string.Template(pconf.read())
            prefix, suffix = os.path.splitext(os.path.basename(cs_profile))
            table_name = "table%s" % idx

            with tempfile.NamedTemporaryFile(mode='w+', prefix=prefix, suffix=suffix, delete=False,
                                             encoding='utf-8') as file_obj:
                output = template.substitute(table_name=table_name)
                file_obj.write(output)
                profile_dst = file_obj.name

            # collect stress command from the comment in the end of the profile yaml
            # example:
            # cassandra-stress user profile={} cl=QUORUM 'ops(insert=1)' duration={} -rate threads=100
            # -pop 'dist=gauss(0..1M)'
            for cmd in [line.lstrip('#').strip() for line in cont if line.find('cassandra-stress') > 0]:
                stress_cmd = (cmd.format(profile_dst, cs_duration))
                params = {'stress_cmd': stress_cmd, 'profile': profile_dst}
                self.log.debug('Stress cmd: {}'.format(stress_cmd))
                params_list.append(params)

        return params_list

    def _run_user_stress_in_batches(self, batch_size, stress_params_list):
        """
        run user profile in batches, while adding 4 stress-commands which are not with precreated tables
        and wait for them to finish

        :param batch_size: size of the batch
        :param stress_params_list: the list of all stress commands
        :return:
        """
        def chunks(_list, chunk_size):
            """Yield successive n-sized chunks from _list."""
            for i in range(0, len(_list), chunk_size):
                yield _list[i:i + chunk_size], i, i+chunk_size, len(_list) + i * 2

        for batch, _, _, extra_tables_idx in list(chunks(stress_params_list, batch_size)):

            stress_queue = list()
            batch_params = dict(round_robin=True, stress_cmd=[])

            # add few stress threads with tables that weren't pre-created
            customer_profiles = self.params.get('cs_user_profiles', default=[])
            for cs_profile in customer_profiles:
                # for now we'll leave to just one fresh table, to kick schema update
                num_of_newly_created_tables = 1
                self._pre_create_templated_user_schema(batch_start=extra_tables_idx,
                                                       batch_end=extra_tables_idx+num_of_newly_created_tables)
                for i in range(num_of_newly_created_tables):
                    batch += self.create_templated_user_stress_params(extra_tables_idx + i, cs_profile=cs_profile)

            for params in batch:
                batch_params['stress_cmd'] += [params['stress_cmd']]

            self._run_all_stress_cmds(stress_queue, params=batch_params)
            for stress in stress_queue:
                self.verify_stress_thread(cs_thread_pool=stress)

    def _run_all_stress_cmds(self, stress_queue, params):
        stress_cmds = params['stress_cmd']
        if not isinstance(stress_cmds, list):
            stress_cmds = [stress_cmds]
        # In some cases we want the same stress_cmd to run several times (can be used with round_robin or not).
        stress_multiplier = self.params.get('stress_multiplier', default=1)
        if stress_multiplier > 1:
            stress_cmds *= stress_multiplier

        for stress_cmd in stress_cmds:
            params.update({'stress_cmd': stress_cmd})
            self._parse_stress_cmd(stress_cmd, params)

            # Run all stress commands
            self.log.debug('stress cmd: {}'.format(stress_cmd))
            if stress_cmd.startswith('scylla-bench'):
                stress_queue.append(self.run_stress_thread_bench(stress_cmd=stress_cmd,
                                                                 stats_aggregate_cmds=False,
                                                                 round_robin=self.params.get('round_robin')))
            else:
                stress_queue.append(self.run_stress_thread(**params))

            time.sleep(10)

            # Remove "user profile" param for the next command
            if 'profile' in params:
                del params['profile']

            if 'keyspace_name' in params:
                del params['keyspace_name']

        return stress_queue

    def _parse_stress_cmd(self, stress_cmd, params):
        # Due to an issue with scylla & cassandra-stress - we need to create the counter table manually
        if 'counter_' in stress_cmd:
            self._create_counter_table()

        if 'compression' in stress_cmd:
            if 'keyspace_name' not in params:
                compression_prefix = re.search('compression=(.*)Compressor', stress_cmd).group(1)
                keyspace_name = "keyspace_{}".format(compression_prefix.lower())
                params.update({'keyspace_name': keyspace_name})

        return params

    def _create_counter_table(self):
        """
        workaround for the issue https://github.com/scylladb/scylla-tools-java/issues/32
        remove when resolved
        """
        node = self.db_cluster.nodes[0]
        with self.cql_connection_patient(node) as session:
            session.execute("""
                CREATE KEYSPACE IF NOT EXISTS keyspace1
                WITH replication = {'class': 'SimpleStrategy', 'replication_factor': '3'} AND durable_writes = true;
            """)
            session.execute("""
                CREATE TABLE IF NOT EXISTS keyspace1.counter1 (
                    key blob PRIMARY KEY,
                    "C0" counter,
                    "C1" counter,
                    "C2" counter,
                    "C3" counter,
                    "C4" counter
                ) WITH COMPACT STORAGE
                    AND bloom_filter_fp_chance = 0.01
                    AND caching = '{"keys":"ALL","rows_per_partition":"ALL"}'
                    AND comment = ''
                    AND compaction = {'class': 'SizeTieredCompactionStrategy'}
                    AND compression = {}
                    AND dclocal_read_repair_chance = 0.1
                    AND default_time_to_live = 0
                    AND gc_grace_seconds = 864000
                    AND max_index_interval = 2048
                    AND memtable_flush_period_in_ms = 0
                    AND min_index_interval = 128
                    AND read_repair_chance = 0.0
                    AND speculative_retry = '99.0PERCENTILE';
            """)

    def bootstrap_test(self):
        add_node_cnt = self.params.get('add_node_cnt', default=1)

        batch_size = self.params.get('batch_size', default=1)

        self._pre_create_templated_user_schema()

        self.add_one_node_and_wait(check_bootstrap_time=False)

        stress_params_list = list()

        customer_profiles = self.params.get('cs_user_profiles', default=[])

        templated_table_counter = itertools.count()

        if customer_profiles:
            cs_duration = self.params.get('cs_duration', default='50m')
            for cs_profile in customer_profiles:
                assert os.path.exists(cs_profile), 'File not found: {}'.format(cs_profile)
                self.log.debug('Run stress test with user profile {}, duration {}'.format(cs_profile, cs_duration))

                user_profile_table_count = self.params.get('user_profile_table_count',  # pylint: disable=invalid-name
                                                           default=1)
                for _ in range(user_profile_table_count):
                    stress_params_list += self.create_templated_user_stress_params(next(templated_table_counter),
                                                                                   cs_profile)

            self._run_user_stress_in_batches(batch_size=batch_size,
                                             stress_params_list=stress_params_list)

        for _ in range(add_node_cnt - 1):
            self.add_one_node_and_wait()
