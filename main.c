/**
 * ============================================================================
 * PARALLEL TX SIMULATOR - Load generator (OpenMP + ZMQ)
 * ============================================================================
 *
 * Uses OpenMP to create batches of transactions in parallel and sends them
 * over ZMQ to a receiver (validator/simulator) process. Ready for future
 * NEAR-style validator nodes.
 *
 * Default: --connect tcp://localhost:5557 (send to receiver).
 * Optional: --in-process (no ZMQ, use callback only; for quick tests).
 * Optional: --spawn-receiver (fork/exec receiver; stopped on generator exit or SIGINT/SIGTERM).
 *
 * Build: make
 * Run:   Start receiver first, then: ./build/generator <from> <to> <amount> <count> [options]
 *
 * ============================================================================
 */

#include "transaction.h"
#include "wallet.h"
#include "common.h"
#include "bench.h"
#include "blockchain.pb-c.h"
#include <stdlib.h>
#include <stdio.h>
#include <string.h>
#include <inttypes.h>
#include <omp.h>
#include <sys/time.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <signal.h>
#include <unistd.h>
#include <zmq.h>

#define MAX_RECEIVER_ARGS 32

static pid_t g_spawned_receiver_pid = -1;

static void stop_spawned_receiver(void) {
    if (g_spawned_receiver_pid > 0) {
        kill(g_spawned_receiver_pid, SIGTERM);
        (void)waitpid(g_spawned_receiver_pid, NULL, 0);
        g_spawned_receiver_pid = -1;
    }
}

static void on_signal_stop_receiver(int sig) {
    stop_spawned_receiver();
    signal(sig, SIG_DFL);
    raise(sig);
}

#define DEFAULT_THREADS  48
#define DEFAULT_BATCH    64
#define MAX_THREADS      64
#define MAX_BATCH_SIZE   256
#define SUBMIT_BATCH_PB_PREFIX "SUBMIT_BATCH_PB:"  /* 16 bytes */
#define DEFAULT_CONNECT  "tcp://localhost:5557"

static unsigned int g_sleep_ms = 0;  /* in-process mode: optional delay per batch */

static double get_time_ms(void) {
    struct timeval tv;
    gettimeofday(&tv, NULL);
    return tv.tv_sec * 1000.0 + tv.tv_usec / 1000.0;
}

/** In-process callback (used only when --in-process). */
static void simulator_submit_batch(Transaction** txs, uint32_t count) {
    (void)txs;
    if (count == 0) return;
    if (g_sleep_ms > 0)
        usleep((useconds_t)(g_sleep_ms * 1000));
}

int main(int argc, char* argv[]) {
    if (argc < 5) {
        fprintf(stderr, "Usage: %s <from> <to> <amount> <count> [options]\n", argv[0]);
        fprintf(stderr, "  --connect ADDR  ZMQ endpoint (default: %s)\n", DEFAULT_CONNECT);
        fprintf(stderr, "  --in-process     No ZMQ; use in-process callback (for testing)\n");
        fprintf(stderr, "  --spawn-receiver Fork/exec receiver; kill it on exit or SIGINT/SIGTERM\n");
        fprintf(stderr, "  --receiver-path P  Receiver binary (default: build/receiver)\n");
        fprintf(stderr, "  --receiver-arg A   Extra argv for receiver (repeat for multiple)\n");
        fprintf(stderr, "  --threads N     OpenMP threads (default: %d)\n", DEFAULT_THREADS);
        fprintf(stderr, "  --batch N       TXs per batch (default: %d)\n", DEFAULT_BATCH);
        fprintf(stderr, "  --sleep-ms N    In-process only: sleep N ms per batch (default: 0)\n");
        fprintf(stderr, "  --base-nonce N  Starting nonce (default: 0)\n");
        fprintf(stderr, "  --bench         Print per-phase timings (see README Benchmarks)\n");
        fprintf(stderr, "  --bench-oneway  ZMQ: append send timestamp; receiver logs bench_oneway_ms (needs matching receiver flag)\n");
        return 1;
    }

    const char* from_name = argv[1];
    const char* to_name   = argv[2];
    uint64_t amount       = strtoull(argv[3], NULL, 10);
    int total_count       = atoi(argv[4]);
    int num_threads       = DEFAULT_THREADS;
    int batch_size        = DEFAULT_BATCH;
    int64_t base_nonce    = 0;
    const char* connect_addr = DEFAULT_CONNECT;
    int use_zmq           = 1;
    int spawn_receiver    = 0;
    const char* receiver_path = "build/receiver";
    const char* receiver_argv[MAX_RECEIVER_ARGS + 1];
    int receiver_argc = 0;
    int bench_mode        = 0;
    int bench_oneway      = 0;

    for (int i = 5; i < argc; i++) {
        if (strcmp(argv[i], "--connect") == 0 && i + 1 < argc) {
            connect_addr = argv[++i];
        } else if (strcmp(argv[i], "--in-process") == 0) {
            use_zmq = 0;
        } else if (strcmp(argv[i], "--spawn-receiver") == 0) {
            spawn_receiver = 1;
        } else if (strcmp(argv[i], "--receiver-path") == 0 && i + 1 < argc) {
            receiver_path = argv[++i];
        } else if (strcmp(argv[i], "--receiver-arg") == 0 && i + 1 < argc) {
            if (receiver_argc >= MAX_RECEIVER_ARGS) {
                fprintf(stderr, "Too many --receiver-arg (max %d)\n", MAX_RECEIVER_ARGS);
                return 1;
            }
            receiver_argv[receiver_argc++] = argv[++i];
        } else if (strcmp(argv[i], "--threads") == 0 && i + 1 < argc) {
            num_threads = atoi(argv[++i]);
        } else if (strcmp(argv[i], "--batch") == 0 && i + 1 < argc) {
            batch_size = atoi(argv[++i]);
        } else if (strcmp(argv[i], "--sleep-ms") == 0 && i + 1 < argc) {
            g_sleep_ms = (unsigned int)atoi(argv[++i]);
        } else if (strcmp(argv[i], "--base-nonce") == 0 && i + 1 < argc) {
            base_nonce = (int64_t)atoll(argv[++i]);
        } else if (strcmp(argv[i], "--bench") == 0) {
            bench_mode = 1;
        } else if (strcmp(argv[i], "--bench-oneway") == 0) {
            bench_oneway = 1;
        }
    }

    if (bench_oneway && !use_zmq) {
        fprintf(stderr, "--bench-oneway requires ZMQ (not --in-process)\n");
        return 1;
    }

    if (spawn_receiver && !use_zmq) {
        fprintf(stderr, "--spawn-receiver cannot be used with --in-process\n");
        return 1;
    }

    if (batch_size > MAX_BATCH_SIZE) batch_size = MAX_BATCH_SIZE;
    if (batch_size < 1) batch_size = 1;
    if (num_threads < 1) num_threads = 1;
    if (num_threads > MAX_THREADS) num_threads = MAX_THREADS;

    uint8_t to_addr[20];
    if (!wallet_parse_address(to_name, to_addr)) {
        fprintf(stderr, "Invalid 'to' address or name: %s\n", to_name);
        return 1;
    }

    uint32_t current_height = 0;
    uint32_t expiry_blocks  = 1000;
    uint32_t fee            = 1;
    int num_batches         = (total_count + batch_size - 1) / batch_size;

    printf("\nParallel TX Generator (OpenMP + %s)\n", use_zmq ? "ZMQ" : "in-process");
    printf("  From: %s  To: %s  Amount: %" PRIu64 "  Count: %d\n", from_name, to_name, amount, total_count);
    printf("  Threads: %d  Batch size: %d  Batches: %d\n", num_threads, batch_size, num_batches);
    if (use_zmq)
        printf("  Connect: %s\n", connect_addr);
    else if (g_sleep_ms > 0)
        printf("  In-process sleep: %u ms per batch\n", g_sleep_ms);
    if (spawn_receiver)
        printf("  Spawn receiver: %s (auto-stop when generator exits)\n", receiver_path);
    if (bench_mode)
        printf("  Benchmark: per-phase sums over batches (parallel work may overlap in wall time)\n");
    if (bench_oneway)
        printf("  Bench one-way: 8-byte CLOCK_REALTIME trailer on each ZMQ batch (receiver must use --bench-oneway)\n");
    printf("\n");

    if (spawn_receiver && use_zmq) {
        receiver_argv[receiver_argc] = NULL;
        pid_t pid = fork();
        if (pid < 0) {
            perror("fork");
            return 1;
        }
        if (pid == 0) {
            char* av[MAX_RECEIVER_ARGS + 2];
            av[0] = (char*)receiver_path;
            for (int r = 0; r < receiver_argc; r++)
                av[r + 1] = (char*)receiver_argv[r];
            av[receiver_argc + 1] = NULL;
            execvp(receiver_path, av);
            perror("execvp receiver");
            _exit(127);
        }
        g_spawned_receiver_pid = pid;
        if (atexit(stop_spawned_receiver) != 0)
            fprintf(stderr, "Warning: atexit(stop_spawned_receiver) failed\n");
        signal(SIGINT, on_signal_stop_receiver);
        signal(SIGTERM, on_signal_stop_receiver);
        sleep(1);
    }

    Wallet** thread_wallets = (Wallet**)safe_malloc((size_t)num_threads * sizeof(Wallet*));
    for (int t = 0; t < num_threads; t++) {
        thread_wallets[t] = wallet_create_named(from_name);
        if (!thread_wallets[t]) {
            fprintf(stderr, "Failed to create wallet for thread %d\n", t);
            for (int j = 0; j < t; j++) wallet_destroy(thread_wallets[j]);
            free(thread_wallets);
            return 1;
        }
    }

    /* Per-thread ZMQ (only when using ZMQ) */
    void** thread_ctxs = NULL;
    void** thread_socks = NULL;
    if (use_zmq) {
        thread_ctxs = (void**)safe_malloc((size_t)num_threads * sizeof(void*));
        thread_socks = (void**)safe_malloc((size_t)num_threads * sizeof(void*));
        for (int t = 0; t < num_threads; t++) {
            thread_ctxs[t] = zmq_ctx_new();
            thread_socks[t] = zmq_socket(thread_ctxs[t], ZMQ_REQ);
            if (zmq_connect(thread_socks[t], connect_addr) != 0) {
                fprintf(stderr, "ZMQ connect failed for thread %d to %s\n", t, connect_addr);
                for (int j = 0; j <= t; j++) {
                    if (thread_socks[j]) zmq_close(thread_socks[j]);
                    if (thread_ctxs[j]) zmq_ctx_destroy(thread_ctxs[j]);
                }
                free(thread_socks); free(thread_ctxs);
                for (int j = 0; j < num_threads; j++) wallet_destroy(thread_wallets[j]);
                free(thread_wallets);
                return 1;
            }
            int timeout = 30000;
            zmq_setsockopt(thread_socks[t], ZMQ_RCVTIMEO, &timeout, sizeof(timeout));
        }
    }

    int* thread_submitted = (int*)safe_malloc((size_t)num_threads * sizeof(int));
    memset(thread_submitted, 0, (size_t)num_threads * sizeof(int));

    double start_time = get_time_ms();
    double sum_build = 0, sum_pack = 0, sum_submit = 0, sum_inproc = 0;

#pragma omp parallel for schedule(dynamic) num_threads(num_threads) \
    reduction(+:sum_build, sum_pack, sum_submit, sum_inproc)
    for (int b = 0; b < num_batches; b++) {
        int tid = omp_get_thread_num();
        Wallet* wallet = thread_wallets[tid];
        int tx_start = b * batch_size;
        int tx_end   = tx_start + batch_size;
        if (tx_end > total_count) tx_end = total_count;
        int batch_count = tx_end - tx_start;

        Transaction** raw_txs = (Transaction**)safe_malloc((size_t)batch_count * sizeof(Transaction*));
        int txs_in_batch = 0;

        double tb0 = 0, tb1 = 0, tb2 = 0, tb3 = 0;
        if (bench_mode)
            tb0 = bench_now_ms();

        for (int i = tx_start; i < tx_end; i++) {
            uint64_t nonce = (uint64_t)(base_nonce + i);
            uint32_t expiry = (expiry_blocks > 0) ? current_height + expiry_blocks : 0;
            Transaction* tx = transaction_create(wallet, to_addr, amount, fee, nonce, expiry);
            if (tx) {
                raw_txs[txs_in_batch++] = tx;
            }
        }

        if (bench_mode)
            tb1 = bench_now_ms();

        if (txs_in_batch > 0) {
            if (use_zmq) {
                Blockchain__Transaction* pb_arr =
                    (Blockchain__Transaction*)safe_malloc((size_t)txs_in_batch * sizeof(Blockchain__Transaction));
                Blockchain__Transaction** pb_ptrs =
                    (Blockchain__Transaction**)safe_malloc((size_t)txs_in_batch * sizeof(Blockchain__Transaction*));
                for (int i = 0; i < txs_in_batch; i++) {
                    blockchain__transaction__init(&pb_arr[i]);
                    pb_arr[i].nonce = raw_txs[i]->nonce;
                    pb_arr[i].expiry_block = raw_txs[i]->expiry_block;
                    pb_arr[i].source_address.data = raw_txs[i]->source_address;
                    pb_arr[i].source_address.len = 20;
                    pb_arr[i].dest_address.data = raw_txs[i]->dest_address;
                    pb_arr[i].dest_address.len = 20;
                    pb_arr[i].value = raw_txs[i]->value;
                    pb_arr[i].fee = raw_txs[i]->fee;
                    if (!is_zero(raw_txs[i]->signature, 48)) {
                        pb_arr[i].signature.data = raw_txs[i]->signature;
                        pb_arr[i].signature.len = 48;
                    }
                    pb_ptrs[i] = &pb_arr[i];
                }
                Blockchain__TransactionBatch batch = BLOCKCHAIN__TRANSACTION_BATCH__INIT;
                batch.n_transactions = (size_t)txs_in_batch;
                batch.transactions = pb_ptrs;
                batch.count = (uint32_t)txs_in_batch;

                size_t pb_size = blockchain__transaction_batch__get_packed_size(&batch);
                size_t msg_size = 16 + pb_size + (bench_oneway ? 8u : 0u);
                uint8_t* msg = (uint8_t*)safe_malloc(msg_size);
                memcpy(msg, SUBMIT_BATCH_PB_PREFIX, 16);
                blockchain__transaction_batch__pack(&batch, msg + 16);
                if (bench_oneway)
                    bench_u64_le_store(msg + 16 + pb_size, bench_realtime_ns());

                if (bench_mode)
                    tb2 = bench_now_ms();

                void* sock = thread_socks[tid];
                zmq_send(sock, msg, msg_size, 0);
                free(msg);

                char resp[128];
                int sz = zmq_recv(sock, resp, sizeof(resp) - 1, 0);
                if (bench_mode)
                    tb3 = bench_now_ms();

                if (sz > 0) {
                    resp[sz] = '\0';
                    if (strncmp(resp, "OK", 2) == 0)
                        thread_submitted[tid] += txs_in_batch;
                }

                free(pb_ptrs);
                free(pb_arr);

                if (bench_mode) {
                    sum_build += tb1 - tb0;
                    sum_pack += tb2 - tb1;
                    sum_submit += tb3 - tb2;
                }
            } else {
                simulator_submit_batch(raw_txs, (uint32_t)txs_in_batch);
                if (bench_mode)
                    tb2 = bench_now_ms();
                thread_submitted[tid] += txs_in_batch;
                if (bench_mode) {
                    sum_build += tb1 - tb0;
                    sum_inproc += tb2 - tb1;
                }
            }
        } else if (bench_mode) {
            /* No txs in batch; only count build time (empty loop). */
            sum_build += tb1 - tb0;
        }

        for (int i = 0; i < txs_in_batch; i++)
            transaction_destroy(raw_txs[i]);
        free(raw_txs);
    }

    double total_time = get_time_ms() - start_time;

    if (use_zmq) {
        for (int t = 0; t < num_threads; t++) {
            zmq_close(thread_socks[t]);
            zmq_ctx_destroy(thread_ctxs[t]);
        }
        free(thread_socks);
        free(thread_ctxs);
    }

    int total_submitted = 0;
    for (int t = 0; t < num_threads; t++)
        total_submitted += thread_submitted[t];

    printf("  Submitted: %d TXs in %.2f ms\n", total_submitted, total_time);
    printf("  Throughput: %.2f tx/sec\n", total_time > 0 ? (total_submitted * 1000.0 / total_time) : 0);
    printf("\n");

    if (bench_mode) {
        double thr = total_time > 0 ? (total_submitted * 1000.0 / total_time) : 0.0;
        bench_generator_report(stdout, total_submitted, num_batches, total_time,
                               sum_build, sum_pack, sum_submit, sum_inproc, use_zmq);
        bench_generator_machine_line(total_time, total_submitted, num_batches,
                                     sum_build, sum_pack, sum_submit, sum_inproc,
                                     use_zmq ? 1 : 0, thr);
    }

    for (int t = 0; t < num_threads; t++)
        wallet_destroy(thread_wallets[t]);
    free(thread_wallets);
    free(thread_submitted);

    return 0;
}
