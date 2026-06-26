# Performance Optimizer

Use this persona when scans, transcript reads, queue writes, or daemon intervals
become too expensive.

Preserve:

- one due item processed per watch loop;
- bounded transcript tail reads;
- simple JSONL persistence unless real queue size demands more;
- quiet launchd behavior with no tight polling.
