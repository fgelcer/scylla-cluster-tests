import time
from invoke import exceptions
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
            if any(values[1] for values in node['values']):
                self.log.info('Admission control was triggered')
                is_admission_control_triggered = True

        return is_admission_control_triggered

    def kill_load(self):
        for loader in self.loaders.nodes:
            cs_process_list = loader.remoter.run('sudo pgrep -f cassandra-stress',
                                                 ignore_status=True).stdout.splitlines()
            self.log.debug(f'c-s process id list = {cs_process_list}')
            processes_to_kill = ' '.join(cs_process_list)
            kill_process_res = loader.remoter.run(f'sudo kill -9 {processes_to_kill}', ignore_status=True)
            self.log.debug(f'kill c-s processes res = {kill_process_res}')

    def run_load(self, job_num, job_cmd, is_prepare=False):
        if is_prepare:
            prepare_stress_queue = self.run_stress_thread(stress_cmd=job_cmd, stress_num=job_num, prefix='preload-',
                                                          stats_aggregate_cmds=False)
            self.get_stress_results(prepare_stress_queue)
            is_ever_triggered = False
        else:
            stress_queue = []
            stress_res = []
            stress_queue.append(self.run_stress_thread(stress_cmd=job_cmd, stress_num=job_num,
                                                       stats_aggregate_cmds=False))

            start_time = time.time()
            is_ever_triggered = False
            while stress_queue:
                stress_res.append(stress_queue.pop(0))
                while not all(future.done() for future in stress_res[-1].results_futures):
                    now = time.time()
                    if self.check_prometheus_metrics(start_time=start_time, now=now):
                        is_ever_triggered = True
                        self.log.info('killing all the load on all loaders')
                        self.kill_load()
                        break
                    start_time = now
                    time.sleep(10)

            if not is_ever_triggered:
                results = []
                for stress in stress_res:
                    try:
                        results.append(self.get_stress_results(stress))
                    except exceptions.CommandTimedOut as ex:
                        self.log.debug('some c-s timed out\n{}'.format(ex))

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

        self.run_admission_control(base_cmd_prepare, base_cmd_r, job_num=12)
        self.log.debug('Finished running read test')

    def write_admission_control_load(self):
        """
        Test steps:
        1. Run a write workload without need to preparation
        """
        self.log.debug('Started running write test')
        base_cmd_w = self.params.get('stress_cmd_w')

        self.run_admission_control(None, base_cmd_w, job_num=12)
        self.log.debug('Finished running write test')

    def test_admission_control(self):
        self.write_admission_control_load()

        # giving some time for the load to calm down
        time.sleep(10)
        node = self.db_cluster.nodes[0]
        node.stop_scylla(verify_up=False, verify_down=True)
        node.start_scylla(verify_up=True)
        self.log.info('will give few seconds to everything come back to it\'s place')
        time.sleep(240)

        self.read_admission_control_load()
        self.log.info('Admission Control Test has finished with success')
