#ifndef BENCH_H
#define BENCH_H

#include <stdio.h>
#include <stdint.h>

/* Wall-clock milliseconds (monotonic enough for same-machine benchmarks). */
double bench_now_ms(void);

/* CLOCK_REALTIME nanoseconds (same host: comparable across processes). */
uint64_t bench_realtime_ns(void);

void bench_u64_le_store(uint8_t* p, uint64_t v);
uint64_t bench_u64_le_load(const uint8_t* p);

/* Receiver: log one-way latency ms (send timestamp was in message trailer). */
void bench_oneway_log_ms(double ms);

/* Generator: print breakdown after a run with --bench. */
void bench_generator_report(FILE* out,
                            int total_tx,
                            int num_batches,
                            double wall_ms,
                            double sum_tx_build_ms,
                            double sum_pb_pack_ms,
                            double sum_submit_ack_ms,
                            double sum_inproc_cb_ms,
                            int zmq_mode);

/* Receiver: one stderr line per batch when benchmarking (for correlation / logs). */
void bench_receiver_batch(double proc_ms, int n_tx);

/* One stdout line for scripts (grep ^BENCH_LINE): machine-parseable results. */
void bench_generator_machine_line(double wall_ms,
                                  int submitted,
                                  int batches,
                                  double sum_build_ms,
                                  double sum_pack_ms,
                                  double sum_submit_ack_ms,
                                  double sum_inproc_ms,
                                  int zmq_mode,
                                  double throughput_tx_s);

#endif
