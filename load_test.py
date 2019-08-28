import time
from sdcm.tester import ClusterTester
from sdcm.db_stats import PrometheusDBStats


class LoadTest(ClusterTester):
    """
    Test Scylla load with cassandra-stress
    """

    def __init__(self, *args, **kwargs):
        super(LoadTest, self).__init__(*args, **kwargs)

    def setUp(self):
        super(LoadTest, self).setUp()
        self.start_time = time.time()

    def run_load_and_check_for_admission_control(self, prepare_base_cmd, stress_base_cmd):
        prometheus = PrometheusDBStats(self.monitors.nodes[0].public_ip_address)
        transport_requests_memory_available = 'scylla_transport_requests_memory_available'
        node_procs_blocked = 'scylla_transport_requests_blocked_memory'

        self.run_fstrim_on_all_db_nodes()
        # run a write workload
        # TODO: if prepare is None, then no run is needed
        prepare_stress_queue = self.run_stress_thread(stress_cmd=prepare_base_cmd, stress_num=2, prefix='preload-',
                                                      stats_aggregate_cmds=False)
        # self.verify_stress_thread(cs_thread_pool=stress_queue)
        self.get_stress_results(prepare_stress_queue)

        time.sleep(10)

        # run a read workload
        self.run_fstrim_on_all_db_nodes()
        stress_queue = []
        stress_res = []
        stress_queue.append(self.run_stress_thread(stress_cmd=stress_base_cmd, stress_num=8, stats_aggregate_cmds=False))

        start_time = time.time()
        max_transport_requests_memory_available = 0
        is_admission_control_triggered = False
        while stress_queue:
            stress_res.append(stress_queue.pop(0))
            while not all(future.done() for future in stress_res[-1].results_futures):
                now = time.time()
                transport_res = prometheus.query(transport_requests_memory_available, start_time, now)
                node_procs_res = prometheus.query(node_procs_blocked, start_time, now)

                sum_values = 0
                for shard in transport_res:
                    sum_values += int(shard['values'][0][1])
                max_transport_requests_memory_available = max(sum_values, max_transport_requests_memory_available)
                if sum_values < max_transport_requests_memory_available * 0.3:
                    max_transport_requests_memory_available = sum_values
                    for node in node_procs_res:
                        if node['values'][0][1] > 0:
                            self.log.debug('Admission control was triggered')
                            is_admission_control_triggered = True
                start_time = now
                time.sleep(10)

        results = []
        for stress in stress_res:
            results.append(self.get_stress_results(stress))
        # results = self.verify_stress_thread(cs_thread_pool=stress_queue)

        self.verify_no_drops_and_errors(starting_from=self.start_time)
        return is_admission_control_triggered

    def test_read_admission_control_under_load(self):
        """
        Test steps:

        1. Run a write workload as a preparation
        2. Run a read workload
        """

        base_cmd_prepare = self.params.get('prepare_write_cmd')
        base_cmd_r = self.params.get('stress_cmd_r')

        self.log.info('test finished with status {}'.format(
            'passed' if self.run_load_and_check_for_admission_control(base_cmd_prepare, base_cmd_r) else 'failed'))

    def test_write_admission_control_under_load(self):
        """
        Test steps:

        1. Run a write workload as a preparation
        """
        # FIXME: might have to increase the write load, as it didn't trigger the admission control as in the read
        base_cmd_prepare = self.params.get('prepare_write_cmd')
        base_cmd_w = self.params.get('stress_cmd_w')

        self.log.info('test finished with status {}'.format(
            'passed' if self.run_load_and_check_for_admission_control(base_cmd_prepare, base_cmd_w) else 'failed'))

    # TODO: might want to create a single test that calls all different loads
