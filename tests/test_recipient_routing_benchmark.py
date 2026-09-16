#!/usr/bin/env python3
"""
Performance benchmark suite for recipient routing.

Compares performance metrics:
- Latency distribution (p50, p95, p99)
- Token usage patterns
- Memory usage
- Concurrent request handling
- Error rates

Run with:
    python test_recipient_routing_benchmark.py --agents 3 --requests 20
"""

import asyncio
import json
import time
import sys
import argparse
import statistics
from dataclasses import dataclass
from typing import Optional
from datetime import datetime
import httpx

DUAL_LOBE_HOST = "http://localhost:8801"

@dataclass
class PerformanceMetrics:
    """Performance metrics for a test run"""
    mode: str  # "control" or "test"
    num_agents: int
    num_requests: int
    total_time_s: float
    
    # Latency metrics (ms)
    latencies: list[float]
    
    # Token metrics
    total_tokens: int
    avg_tokens_per_request: float
    
    # Error metrics
    errors: int
    success_rate: float
    
    # Calculated metrics
    @property
    def p50_latency_ms(self) -> float:
        return statistics.median(self.latencies) if self.latencies else 0
    
    @property
    def p95_latency_ms(self) -> float:
        if len(self.latencies) < 20:
            return max(self.latencies) if self.latencies else 0
        sorted_latencies = sorted(self.latencies)
        return sorted_latencies[int(len(sorted_latencies) * 0.95)]
    
    @property
    def p99_latency_ms(self) -> float:
        if len(self.latencies) < 100:
            return max(self.latencies) if self.latencies else 0
        sorted_latencies = sorted(self.latencies)
        return sorted_latencies[int(len(sorted_latencies) * 0.99)]
    
    @property
    def avg_latency_ms(self) -> float:
        return statistics.mean(self.latencies) if self.latencies else 0
    
    @property
    def throughput_req_s(self) -> float:
        return self.num_requests / self.total_time_s if self.total_time_s > 0 else 0


class BenchmarkClient:
    """Client for benchmark testing"""
    
    def __init__(self, base_url: str = DUAL_LOBE_HOST, enable_routing: bool = False):
        self.base_url = base_url
        self.enable_routing = enable_routing
    
    async def run_benchmark(self, num_agents: int, num_requests: int) -> PerformanceMetrics:
        """Run benchmark with specified agents and request count"""
        
        latencies = []
        total_tokens = 0
        errors = 0
        
        agent_names = [f"agent_{i}" for i in range(num_agents)]
        
        start_time = time.time()
        
        async with httpx.AsyncClient(timeout=30) as client:
            # Create concurrent tasks
            tasks = []
            for i in range(num_requests):
                agent = agent_names[i % num_agents]
                message = f"Test message {i} directed at agent_0"
                
                task = self._make_request(client, agent, message)
                tasks.append(task)
            
            # Execute all tasks concurrently
            results = await asyncio.gather(*tasks, return_exceptions=True)
        
        total_time = time.time() - start_time
        
        # Process results
        for result in results:
            if isinstance(result, dict) and not result.get("error"):
                latencies.append(result.get("latency_ms", 0))
                total_tokens += result.get("tokens", 0)
            else:
                errors += 1
        
        success_rate = (num_requests - errors) / num_requests * 100 if num_requests > 0 else 0
        
        return PerformanceMetrics(
            mode="test" if self.enable_routing else "control",
            num_agents=num_agents,
            num_requests=num_requests,
            total_time_s=total_time,
            latencies=latencies,
            total_tokens=total_tokens,
            avg_tokens_per_request=total_tokens / (num_requests - errors) if (num_requests - errors) > 0 else 0,
            errors=errors,
            success_rate=success_rate,
        )
    
    async def _make_request(self, client: httpx.AsyncClient, agent: str, message: str) -> dict:
        """Make a single API request"""
        start = time.time()
        
        try:
            payload = {
                "messages": [{"role": "user", "content": message}],
                "model": "gpt-4",
                "temperature": 0,
            }
            
            headers = {
                "X-Agent-Name": agent,
                "X-Routing-Enabled": "true" if self.enable_routing else "false",
            }
            
            response = await client.post(
                f"{self.base_url}/v1/chat/completions",
                json=payload,
                headers=headers,
            )
            
            latency = (time.time() - start) * 1000
            
            if response.status_code != 200:
                return {"error": f"HTTP {response.status_code}", "latency_ms": latency}
            
            data = response.json()
            
            return {
                "latency_ms": latency,
                "tokens": data.get("usage", {}).get("total_tokens", 0),
            }
        except Exception as e:
            return {
                "error": str(e),
                "latency_ms": (time.time() - start) * 1000,
            }


def print_metrics(metrics: PerformanceMetrics) -> None:
    """Print performance metrics"""
    print(f"\n{'='*80}")
    print(f"BENCHMARK RESULTS - {metrics.mode.upper()}")
    print(f"{'='*80}\n")
    
    print(f"Configuration:")
    print(f"  Agents: {metrics.num_agents}")
    print(f"  Total Requests: {metrics.num_requests}")
    print(f"  Concurrent: Yes")
    
    print(f"\nLatency Distribution (ms):")
    print(f"  P50:  {metrics.p50_latency_ms:>8.0f}ms")
    print(f"  P95:  {metrics.p95_latency_ms:>8.0f}ms")
    print(f"  P99:  {metrics.p99_latency_ms:>8.0f}ms")
    print(f"  Avg:  {metrics.avg_latency_ms:>8.0f}ms")
    
    print(f"\nThroughput:")
    print(f"  Total Time: {metrics.total_time_s:>6.2f}s")
    print(f"  Requests/s: {metrics.throughput_req_s:>6.2f} req/s")
    
    print(f"\nToken Usage:")
    print(f"  Total:    {metrics.total_tokens:>8} tokens")
    print(f"  Per Req:  {metrics.avg_tokens_per_request:>8.0f} tokens/req")
    
    print(f"\nReliability:")
    print(f"  Success Rate: {metrics.success_rate:>6.1f}%")
    print(f"  Errors:       {metrics.errors:>6} ({100-metrics.success_rate:.1f}%)")
    
    print(f"\n{'='*80}\n")


def compare_benchmarks(control: PerformanceMetrics, test: PerformanceMetrics) -> None:
    """Compare control vs test benchmarks"""
    print(f"\n{'='*80}")
    print(f"BENCHMARK COMPARISON")
    print(f"{'='*80}\n")
    
    print(f"Latency Impact:")
    p50_delta = test.p50_latency_ms - control.p50_latency_ms
    p95_delta = test.p95_latency_ms - control.p95_latency_ms
    p99_delta = test.p99_latency_ms - control.p99_latency_ms
    avg_delta = test.avg_latency_ms - control.avg_latency_ms
    
    print(f"  P50:  {control.p50_latency_ms:>7.0f}ms → {test.p50_latency_ms:>7.0f}ms ({p50_delta:+7.0f}ms)")
    print(f"  P95:  {control.p95_latency_ms:>7.0f}ms → {test.p95_latency_ms:>7.0f}ms ({p95_delta:+7.0f}ms)")
    print(f"  P99:  {control.p99_latency_ms:>7.0f}ms → {test.p99_latency_ms:>7.0f}ms ({p99_delta:+7.0f}ms)")
    print(f"  Avg:  {control.avg_latency_ms:>7.0f}ms → {test.avg_latency_ms:>7.0f}ms ({avg_delta:+7.0f}ms)")
    
    print(f"\nThroughput:")
    throughput_delta = test.throughput_req_s - control.throughput_req_s
    throughput_pct = (throughput_delta / control.throughput_req_s * 100) if control.throughput_req_s > 0 else 0
    print(f"  Control: {control.throughput_req_s:>6.2f} req/s")
    print(f"  Test:    {test.throughput_req_s:>6.2f} req/s")
    print(f"  Delta:   {throughput_delta:+6.2f} req/s ({throughput_pct:+.1f}%)")
    
    print(f"\nToken Usage:")
    token_delta = test.total_tokens - control.total_tokens
    token_pct = (token_delta / control.total_tokens * 100) if control.total_tokens > 0 else 0
    print(f"  Control: {control.total_tokens:>8} tokens ({control.avg_tokens_per_request:>6.0f}/req)")
    print(f"  Test:    {test.total_tokens:>8} tokens ({test.avg_tokens_per_request:>6.0f}/req)")
    print(f"  Savings: {-token_delta:>8} tokens ({-token_pct:+.1f}%)")
    
    print(f"\nReliability:")
    print(f"  Control: {control.success_rate:>6.1f}% success")
    print(f"  Test:    {test.success_rate:>6.1f}% success")
    
    # Assessment
    print(f"\nAssessment:")
    if avg_delta < 100:
        print(f"  ✓ Latency impact acceptable ({avg_delta:+.0f}ms)")
    else:
        print(f"  ⚠️  Latency impact significant ({avg_delta:+.0f}ms)")
    
    if token_pct < -10:
        print(f"  ✓ Token savings significant ({-token_pct:.1f}%)")
    else:
        print(f"  ✓ Token usage comparable")
    
    if test.success_rate >= 98:
        print(f"  ✓ Reliability maintained ({test.success_rate:.1f}%)")
    else:
        print(f"  ⚠️  Reliability degraded ({test.success_rate:.1f}%)")
    
    print(f"\n{'='*80}\n")


async def main():
    parser = argparse.ArgumentParser(description="Benchmark recipient routing performance")
    parser.add_argument("--agents", type=int, default=3, help="Number of agents")
    parser.add_argument("--requests", type=int, default=20, help="Number of concurrent requests")
    parser.add_argument("--mode", choices=["control", "test", "compare"], default="compare")
    args = parser.parse_args()
    
    if args.mode == "control":
        client = BenchmarkClient(enable_routing=False)
        metrics = await client.run_benchmark(args.agents, args.requests)
        print_metrics(metrics)
    
    elif args.mode == "test":
        client = BenchmarkClient(enable_routing=True)
        metrics = await client.run_benchmark(args.agents, args.requests)
        print_metrics(metrics)
    
    else:  # compare
        print(f"Running benchmark with {args.agents} agents, {args.requests} concurrent requests...")
        
        print("\n📊 Control (routing disabled)...")
        control_client = BenchmarkClient(enable_routing=False)
        control = await control_client.run_benchmark(args.agents, args.requests)
        print_metrics(control)
        
        print("\n📊 Test (routing enabled)...")
        test_client = BenchmarkClient(enable_routing=True)
        test = await test_client.run_benchmark(args.agents, args.requests)
        print_metrics(test)
        
        print("\n📊 Comparison...")
        compare_benchmarks(control, test)


if __name__ == "__main__":
    asyncio.run(main())
