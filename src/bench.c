#include "bench.h"
#include <sys/time.h>
#include <time.h>

double bench_now_ms(void) {
    struct timeval tv;
    gettimeofday(&tv, NULL);
    return tv.tv_sec * 1000.0 + tv.tv_usec / 1000.0;
}

uint64_t bench_realtime_ns(void) {
    struct timespec ts;
    if (clock_gettime(CLOCK_REALTIME, &ts) != 0)
        return 0;
    return (uint64_t)ts.tv_sec * 1000000000ULL + (uint64_t)ts.tv_nsec;
}

void bench_u64_le_store(uint8_t* p, uint64_t v) {
    for (int i = 0; i < 8; i++) {
        p[i] = (uint8_t)(v & 0xffu);
        v >>= 8;
    }
}

uint64_t bench_u64_le_load(const uint8_t* p) {
    uint64_t v = 0;
    for (int i = 0; i < 8; i++)
        v |= (uint64_t)p[i] << (8 * i);
    return v;
}

void bench_oneway_log_ms(double ms) {
    fprintf(stderr, "bench_oneway_ms=%.6f\n", ms);
}

void bench_generator_report(FILE* out,
                            int total_tx,
                            int num_batches,
                            double wall_ms,
                            double sum_tx_build_ms,
                            double sum_pb_pack_ms,
                            double sum_submit_ack_ms,
                            double sum_inproc_cb_ms,
                            int zmq_mode) {
    fprintf(out, "\n--- Generator benchmark (sums over all batches) ---\n");
    fprintf(out, "  Wall time (parallel region):     %.3f ms\n", wall_ms);
    fprintf(out, "  Batches:                           %d\n", num_batches);
    fprintf(out, "  Sum tx build (create+sign):        %.3f ms\n", sum_tx_build_ms);
    if (zmq_mode) {
        fprintf(out, "  Sum protobuf pack:                 %.3f ms\n", sum_pb_pack_ms);
        fprintf(out, "  Sum submit→OK (ZMQ send+recv):     %.3f ms\n", sum_submit_ack_ms);
        fprintf(out, "    (includes receiver work + link; REQ/REP round-trip per batch)\n");
    } else {
        fprintf(out, "  Sum in-process callback:           %.3f ms\n", sum_inproc_cb_ms);
    }
    if (total_tx > 0) {
        fprintf(out, "  Avg build per TX (sum/total_tx):   %.4f ms\n",
                sum_tx_build_ms / (double)total_tx);
        if (zmq_mode)
            fprintf(out, "  Avg submit→OK per TX:              %.4f ms\n",
                    sum_submit_ack_ms / (double)total_tx);
    }
    fprintf(out, "  Note: batch sums can exceed wall time when threads run batches in parallel.\n\n");
}

void bench_receiver_batch(double proc_ms, int n_tx) {
    fprintf(stderr, "bench_recv proc_ms=%.3f txs=%d\n", proc_ms, n_tx);
}

void bench_generator_machine_line(double wall_ms,
                                  int submitted,
                                  int batches,
                                  double sum_build_ms,
                                  double sum_pack_ms,
                                  double sum_submit_ack_ms,
                                  double sum_inproc_ms,
                                  int zmq_mode,
                                  double throughput_tx_s) {
    printf("BENCH_LINE wall_ms=%.6f submitted=%d batches=%d "
           "sum_build_ms=%.6f sum_pack_ms=%.6f sum_submit_ack_ms=%.6f sum_inproc_ms=%.6f "
           "zmq=%d throughput_tx_s=%.6f\n",
           wall_ms, submitted, batches, sum_build_ms, sum_pack_ms, sum_submit_ack_ms,
           sum_inproc_ms, zmq_mode, throughput_tx_s);
    fflush(stdout);
}
