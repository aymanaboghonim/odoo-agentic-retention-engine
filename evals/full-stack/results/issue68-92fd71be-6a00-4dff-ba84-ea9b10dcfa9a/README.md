# Synthetic full-stack capacity measurement

- Run: `92fd71be-6a00-4dff-ba84-ea9b10dcfa9a`
- Attempt: `recovery`
- Host: Linux-5.15.153.1-microsoft-standard-WSL2-x86_64-with-glibc2.39; logical CPUs: 8
- Idle samples: 3; max CPU: 4.41%; max memory: 501.0 MiB
- Scan samples: 3; max CPU: 6.49%; max memory: 501.1 MiB
- Proposal samples: 18; max CPU: 70.46%; max memory: 2049.2 MiB
- Stage durations: scan 5.07s; proposal 53.08s
- CPU is the sum of Docker per-container percentages (100% is one CPU); memory is the sum of Docker-reported per-container working-set values. Samples are sequential snapshots with a one-second pause plus Docker command runtime, not simultaneous peaks.
- The bounded scan and at most one authorized proposal were measured in separate sequential stages. No message or coupon is sent.
- This is not a throughput, production sizing, cost billing, or concurrency test. Token-count billing and active provider limits are unknown.
- [Raw samples and provenance](raw.json)
