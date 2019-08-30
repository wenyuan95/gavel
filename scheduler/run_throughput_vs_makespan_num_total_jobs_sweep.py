import argparse
import datetime
import json
import contextlib
from func_timeout import func_timeout, FunctionTimedOut
import multiprocessing
import numpy as np
import os
import sys

from job_id_pair import JobIdPair
import scheduler
import utils


def emulate_with_timeout(experiment_id, policy_name, schedule_in_rounds,
                         throughputs_file, cluster_spec, lam, seed, interval,
                         fixed_job_duration, generate_multi_gpu_jobs, num_total_jobs,
                         log_dir, timeout, verbose):
    num_total_jobs_str = 'num_total_jobs=%d.log' % (num_total_jobs)
    with open(os.path.join(log_dir, num_total_jobs_str), 'w') as f:
        with contextlib.redirect_stdout(f):
            policy = utils.get_policy(policy_name, seed)
            sched = scheduler.Scheduler(
                            policy,
                            schedule_in_rounds=schedule_in_rounds,
                            throughputs_file=throughputs_file,
                            seed=seed,
                            time_per_iteration=interval,
                            emulate=True)

            cluster_spec_str = 'v100:%d|p100:%d|k80:%d' % (cluster_spec['v100'],
                                                           cluster_spec['p100'],
                                                           cluster_spec['k80'])
            if verbose:
                current_time = datetime.datetime.now()
                print('[%s] [Experiment ID: %2d] '
                      'Configuration: cluster_spec=%s, policy=%s, '
                       'seed=%d, num_total_jobs=%d' % (current_time, experiment_id,
                                                       cluster_spec_str, policy.name,
                                                       seed, num_total_jobs),
                      file=sys.stderr)

            if timeout is None:
                sched.emulate(cluster_spec, lam=lam,
                              fixed_job_duration=fixed_job_duration,
                              generate_multi_gpu_jobs=generate_multi_gpu_jobs,
                              num_total_jobs=num_total_jobs)
                average_jct = sched.get_average_jct()
                utilization = sched.get_cluster_utilization()
                makespan = sched.get_current_timestamp()
            else:
                try:
                    func_timeout(timeout, sched.emulate,
                                 args=(cluster_spec,),
                                 kwargs={
                                    'lam': lam,
                                    'fixed_job_duration': fixed_job_duration,
                                    'generate_multi_gpu_jobs': generate_multi_gpu_jobs,
                                    'num_total_jobs': num_total_jobs
                                 })
                    average_jct = sched.get_average_jct()
                    utilization = sched.get_cluster_utilization()
                    makespan = sched.get_current_timestamp()
                except FunctionTimedOut:
                    average_jct = float('inf')
                    utilization = 1.0

    if verbose:
        current_time = datetime.datetime.now()
        print('[%s] [Experiment ID: %2d] '
              'Results: average JCT=%f, utilization=%f, makespan=%f' % (
                  current_time,
                  experiment_id,
                  average_jct,
                  utilization,
                  makespan),
              file=sys.stderr)

    return average_jct, utilization

def main(args):
    if ((args.num_total_jobs_lower_bound is None and
         args.num_total_jobs_upper_bound is not None) or
        (args.num_total_jobs_lower_bound is not None and
         args.num_total_jobs_upper_bound is None)):
        raise ValueError('If num_total_jobs range is not None, both '
                         'bounds must be specified.')
    schedule_in_rounds = True
    throughputs_file = args.throughputs_file
    num_v100s = args.gpus
    policy_names = args.policies
    experiment_id = 0

    with open(throughputs_file, 'r') as f:
        throughputs = json.load(f)

    raw_logs_dir = os.path.join(args.log_dir, 'raw_logs')
    if not os.path.isdir(raw_logs_dir):
        os.mkdir(raw_logs_dir)

    all_args_list = []
    for ratio_str in args.ratios:
        ratio = {}
        x = ratio_str.split(':')
        if len(x) != 3:
            raise ValueError('Invalid cluster ratio %s' % (ratio_str))
        ratio = {
            'v100': int(x[0]),
            'p100': int(x[1]),
            'k80': int(x[2])
            }
        cluster_spec = {}
        total_gpu_fraction = sum([ratio[gpu_type] for gpu_type in ratio])
        for gpu_type in ratio:
            fraction = ratio[gpu_type] / total_gpu_fraction
            cluster_spec[gpu_type] = int(fraction * num_v100s)

        cluster_spec_str = 'v100=%d.p100=%d.k80=%d' % (cluster_spec['v100'],
                                                       cluster_spec['p100'],
                                                       cluster_spec['k80'])
        raw_logs_cluster_spec_subdir = os.path.join(raw_logs_dir,
                                                    cluster_spec_str)
        if not os.path.isdir(raw_logs_cluster_spec_subdir):
            os.mkdir(raw_logs_cluster_spec_subdir)

        for policy_name in policy_names:
            raw_logs_policy_subdir = os.path.join(raw_logs_cluster_spec_subdir,
                                                  policy_name)
            if not os.path.isdir(raw_logs_policy_subdir):
                os.mkdir(raw_logs_policy_subdir)

            lower_bound = args.num_total_jobs_lower_bound
            upper_bound = args.num_total_jobs_upper_bound
            step = (upper_bound - lower_bound) // args.num_data_points
            all_num_total_jobs = list(np.arange(lower_bound,
                                                upper_bound,
                                                step=step))
            if all_num_total_jobs[0] == 0:
                all_num_total_jobs = all_num_total_jobs[1:]
            for num_total_jobs in all_num_total_jobs:
                lam = 0.0  # All jobs are added at the start of the trace.
                for seed in args.seeds:
                    seed_str = 'seed=%d' % (seed)
                    raw_logs_seed_subdir = \
                            os.path.join(raw_logs_policy_subdir, seed_str)
                    if not os.path.isdir(raw_logs_seed_subdir):
                        os.mkdir(raw_logs_seed_subdir)
                    all_args_list.append((experiment_id, policy_name,
                                          schedule_in_rounds,
                                          throughputs_file, cluster_spec,
                                          lam, seed, args.interval,
                                          args.fixed_job_duration,
                                          args.generate_multi_gpu_jobs,
                                          num_total_jobs,
                                          raw_logs_seed_subdir,
                                          args.timeout, args.verbose))
                    experiment_id += 1
    if len(all_args_list) > 0:
        current_time = datetime.datetime.now()
        print('[%s] Running %d total experiment(s)...' % (current_time,
                                                          len(all_args_list)))
        with multiprocessing.Pool(args.processes) as p:
            # Sort args in order of increasing num_total_jobs to prioritize
            # short-running jobs.
            all_args_list.sort(key=lambda x: x[9])
            results = [p.apply_async(emulate_with_timeout, args_list)
                       for args_list in all_args_list]
            results = [result.get() for result in results]
    else:
        raise ValueError('No work to be done!')

if __name__=='__main__':
    parser = argparse.ArgumentParser(
            description='Sweep through lambda values')
    fixed_range = parser.add_argument_group('Sweep over fixed range')

    parser.add_argument('-g', '--gpus', type=int, default=25,
                        help='Total number of GPUs')
    parser.add_argument('-l', '--log-dir', type=str, default='logs',
                        help='Log directory')
    parser.add_argument('-t', '--timeout', type=int, default=None,
                        help='Timeout (in seconds) for each run')
    parser.add_argument('-j', '--processes', type=int, default=None,
                        help=('Number of processes to use in pool '
                              '(use as many as available if not specified)'))
    parser.add_argument('-p', '--policies', type=str, nargs='+',
                        default=['fifo', 'fifo_perf', 'fifo_packed',
                                 'max_min_fairness', 'max_min_fairness_perf',
                                 'max_min_fairness_packed'],
                        help='List of policies to sweep')
    parser.add_argument('-r', '--ratios', type=str, nargs='+',
                        default=['1:0:0', '1:1:0', '1:1:1', '2:1:0'],
                        help=('List of cluster ratios to sweep in the form '
                              '#v100s:#p100s:#k80s'))
    parser.add_argument('--seeds', type=int, nargs='+',
                        default=[0, 1, 42, 1234, 10],
                        help='List of random seeds')
    parser.add_argument('-i', '--interval', type=int, default=1920,
                        help='Interval length (in seconds)')
    parser.add_argument('-f', '--fixed-job-duration', type=int, default=None,
                        help=('If set, fixes the duration of all jobs to the '
                              'specified value (in seconds)'))
    parser.add_argument('--throughputs_file', type=str,
                        default='oracle_throughputs.json',
                        help='Oracle throughputs file')
    parser.add_argument('-m', '--generate-multi-gpu-jobs', action='store_true', default=False,
                        help=('If set, generates multi-GPU jobs according to '
                              'a pre-defined distribution'))
    parser.add_argument('-v', '--verbose', action='store_true', default=True,
                        help='Verbose')
    fixed_range.add_argument('-a', '--num-total-jobs-lower-bound', type=int,
                             default=None,
                             help='Lower bound for num_total_jobs to sweep')
    fixed_range.add_argument('-b', '--num-total-jobs-upper-bound', type=int,
                             default=None,
                             help='Upper bound for num_total_jobs to sweep')
    fixed_range.add_argument('-n', '--num-data-points', type=int, default=20,
                             help='Number of data points to sweep through')
    args = parser.parse_args()
    main(args)