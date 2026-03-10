/**
 * ============================================================================
 * RECEIVER - Validator/simulator stub (ZMQ REP)
 * ============================================================================
 *
 * Binds a ZMQ REP socket and receives transaction batches from the generator.
 * Unpacks each batch, optionally sleeps (simulated verification time) and
 * optionally verifies each transaction; replies OK. Ready to be extended
 * into a NEAR-style validator node.
 *
 * Run first, then start the generator with --connect tcp://localhost:5557
 *
 * ============================================================================
 */

#include "transaction.h"
#include "common.h"
#include "blockchain.pb-c.h"
#include <stdlib.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>
#include <zmq.h>

#define SUBMIT_BATCH_PB_PREFIX "SUBMIT_BATCH_PB:"  /* 16 bytes */
#define DEFAULT_BIND "tcp://*:5557"
#define RECV_BUF_SIZE (2 * 1024 * 1024)

static unsigned int g_sleep_ms = 0;
static int g_verify = 0;

static Transaction* pb_to_tx(const Blockchain__Transaction* pt) {
    Transaction* tx = (Transaction*)safe_malloc(sizeof(Transaction));
    memset(tx, 0, sizeof(Transaction));
    tx->nonce = pt->nonce;
    tx->expiry_block = pt->expiry_block;
    if (pt->source_address.data && pt->source_address.len >= 20)
        memcpy(tx->source_address, pt->source_address.data, 20);
    if (pt->dest_address.data && pt->dest_address.len >= 20)
        memcpy(tx->dest_address, pt->dest_address.data, 20);
    tx->value = pt->value;
    tx->fee = pt->fee;
    if (pt->signature.data && pt->signature.len > 0) {
        size_t n = pt->signature.len < 48 ? pt->signature.len : 48;
        memcpy(tx->signature, pt->signature.data, n);
    }
    return tx;
}

int main(int argc, char* argv[]) {
    const char* bind_addr = DEFAULT_BIND;

    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--bind") == 0 && i + 1 < argc) {
            bind_addr = argv[++i];
        } else if (strcmp(argv[i], "--sleep-ms") == 0 && i + 1 < argc) {
            g_sleep_ms = (unsigned int)atoi(argv[++i]);
        } else if (strcmp(argv[i], "--verify") == 0) {
            g_verify = 1;
        }
    }

    printf("\nReceiver (validator/simulator stub)\n");
    printf("  Bind: %s\n", bind_addr);
    if (g_sleep_ms > 0) printf("  Sleep: %u ms per batch (simulated verification)\n", g_sleep_ms);
    if (g_verify) printf("  Verify: run transaction_verify() on each TX\n");
    printf("  Run generator with: --connect tcp://localhost:5557\n\n");

    void* ctx = zmq_ctx_new();
    void* rep = zmq_socket(ctx, ZMQ_REP);
    if (zmq_bind(rep, bind_addr) != 0) {
        fprintf(stderr, "Failed to bind %s: %s\n", bind_addr, zmq_strerror(zmq_errno()));
        zmq_close(rep);
        zmq_ctx_destroy(ctx);
        return 1;
    }

    char* buf = (char*)safe_malloc(RECV_BUF_SIZE);
    uint64_t total_received = 0;

    while (1) {
        int size = zmq_recv(rep, buf, RECV_BUF_SIZE - 1, 0);
        if (size < 0) {
            fprintf(stderr, "zmq_recv: %s\n", zmq_strerror(zmq_errno()));
            continue;
        }

        if (size > 16 && memcmp(buf, SUBMIT_BATCH_PB_PREFIX, 16) == 0) {
            Blockchain__TransactionBatch* batch = blockchain__transaction_batch__unpack(
                NULL, (size_t)(size - 16), (uint8_t*)(buf + 16));
            int accepted = 0, rejected = 0;
            if (batch) {
                if (g_sleep_ms > 0)
                    usleep((useconds_t)(g_sleep_ms * 1000));
                for (size_t i = 0; i < batch->n_transactions; i++) {
                    Transaction* tx = pb_to_tx(batch->transactions[i]);
                    if (g_verify && !transaction_verify(tx)) {
                        rejected++;
                    } else {
                        accepted++;
                    }
                    transaction_destroy(tx);
                }
                total_received += (uint64_t)(accepted + rejected);
                blockchain__transaction_batch__free_unpacked(batch, NULL);
            }
            {
                char resp[64];
                snprintf(resp, sizeof(resp), "OK:%d|%d", accepted, rejected);
                zmq_send(rep, resp, (size_t)strlen(resp), 0);
            }
        } else {
            zmq_send(rep, "UNKNOWN", 7, 0);
        }
    }

    free(buf);
    zmq_close(rep);
    zmq_ctx_destroy(ctx);
    (void)total_received;
    return 0;
}

