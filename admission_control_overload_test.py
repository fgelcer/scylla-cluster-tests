import time
from sdcm.tester import ClusterTester
from sdcm.db_stats import PrometheusDBStats


class AdmissionControlOverloadTest(ClusterTester):
    """
    Test Scylla load with cassandra-stress to trigger admission control for both read and write
    """
    max_transport_requests = 0

    def check_prometheus_metrics(self, start_time, now):
        prometheus = PrometheusDBStats(self.monitors.nodes[0].public_ip_address)
        node_procs_blocked = 'scylla_transport_requests_blocked_memory'
        node_procs_res = prometheus.query(node_procs_blocked, start_time, now)

        is_admission_control_triggered = False
        for node in node_procs_res:
            if any(int(values[1]) for values in node['values']):
                self.log.info(f"metric={node_procs_blocked} | value={node['values']}")
                self.log.info('Admission control was triggered')
                is_admission_control_triggered = True
                break

        return is_admission_control_triggered

    def run_load(self, job_num, job_cmd, is_prepare=False):
        if is_prepare:
            prepare_stress_queue = self.run_stress_thread(stress_cmd=job_cmd, stress_num=job_num, prefix='preload-',
                                                          stats_aggregate_cmds=False)
            self.get_stress_results(prepare_stress_queue)
            is_ever_triggered = False
        else:
            stress_queue = self.run_stress_thread(stress_cmd=job_cmd, stress_num=job_num,
                                                  stats_aggregate_cmds=False)

            start_time = time.time()
            is_ever_triggered = False

            # wait 2 minutes to let all the stress to start
            time.sleep(120)

            while not all(future.done() for future in list(stress_queue.results_futures)):
                now = time.time()
                if self.check_prometheus_metrics(start_time=start_time, now=now):
                    is_ever_triggered = True
                    self.log.info('killing all the load on all loaders')
                    stress_queue.kill()
                    break
                start_time = now
                time.sleep(10)
            else:
                self.get_stress_results(queue=stress_queue, store_results=False)

        return is_ever_triggered

    def run_admission_control(self, prepare_base_cmd, stress_base_cmd, job_num):
        # run a write workload
        if prepare_base_cmd:
            self.run_load(job_num=job_num, job_cmd=prepare_base_cmd, is_prepare=True)
            time.sleep(10)

        # run workload load
        is_admission_control_triggered = self.run_load(job_num=job_num, job_cmd=stress_base_cmd)

        self.verify_no_drops_and_errors(starting_from=self.start_time)
        self.assertTrue(is_admission_control_triggered, 'Admission Control wasn\'t triggered')

    def read_admission_control_load(self):
        """
        Test steps:
        1. Run a write workload as a preparation
        2. Run a read workload
        """
        self.log.debug('Started running read test')
        base_cmd_prepare = self.params.get('prepare_write_cmd')
        base_cmd_r = self.params.get('stress_cmd_r')

        # self.run_admission_control(base_cmd_prepare, base_cmd_r, job_num=12)
        self.run_admission_control(None, base_cmd_r, job_num=16)
        self.log.debug('Finished running read test')

    def write_admission_control_load(self):
        """
        Test steps:
        1. Run a write workload without need to preparation
        """
        self.log.debug('Started running write test')
        base_cmd_w = self.params.get('stress_cmd_w')

        self.run_admission_control(None, base_cmd_w, job_num=10)
        self.log.debug('Finished running write test')

    def test_admission_control(self):
        self.write_admission_control_load()

        self.log.info('will give few seconds to everything settle down')
        time.sleep(120)

        self.log.info('restarting scylla-server')
        node = self.db_cluster.nodes[0]
        node.restart_scylla_server(ignore_status=True)

        self.log.info('giving 1 minute as JMX issue workaround')
        time.sleep(60)

        self.read_admission_control_load()
        self.log.info('Admission Control Test has finished with success')
