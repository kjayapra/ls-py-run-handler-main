import asyncio
import random
import string
import uuid
import time

import orjson
import pytest
import pytest_asyncio
from httpx import AsyncClient

from ls_py_handler.main import app


def generate_advanced_run(size_kb=10, complexity="medium"):
    """Generate runs with varying complexity patterns."""
    if complexity == "simple":
        return {
            "trace_id": str(uuid.uuid4()),
            "name": f"Simple Run {uuid.uuid4()}",
            "inputs": {"query": "x" * (size_kb * 100)},
            "outputs": {"result": "y" * (size_kb * 100)},
            "metadata": {"size": size_kb}
        }
    elif complexity == "complex":
        # Nested structures with arrays and objects
        return {
            "trace_id": str(uuid.uuid4()),
            "name": f"Complex Run {uuid.uuid4()}",
            "inputs": {
                "nested": {
                    "level1": {
                        "level2": {
                            "data": "x" * (size_kb * 50),
                            "array": [{"item": i, "data": "a" * 10} for i in range(20)]
                        }
                    }
                },
                "top_level": "b" * (size_kb * 50)
            },
            "outputs": {
                "processed": {
                    "results": [{"metric": i, "value": "c" * 20} for i in range(30)],
                    "summary": "d" * (size_kb * 100)
                }
            },
            "metadata": {
                "complexity": "high",
                "processing_time": random.uniform(0.1, 2.0),
                "model_info": {"version": "1.0", "details": "e" * (size_kb * 50)}
            }
        }
    else:  # medium
        return {
            "trace_id": str(uuid.uuid4()),
            "name": f"Medium Run {uuid.uuid4()}",
            "inputs": {
                "prompt": "x" * (size_kb * 200),
                "config": {"temp": 0.7, "tokens": 100}
            },
            "outputs": {
                "response": "y" * (size_kb * 300),
                "metrics": {"tokens": 150, "time": 1.5}
            },
            "metadata": {
                "model": "gpt-4",
                "timestamp": time.time(),
                "extra": "z" * (size_kb * 100)
            }
        }


@pytest_asyncio.fixture
async def client():
    """Fixture that creates an async test client."""
    async with AsyncClient(app=app, base_url="http://test") as client:
        yield client


@pytest.fixture
def aio_benchmark(benchmark):
    """Async benchmark fixture."""
    def _wrapper(func, *args, **kwargs):
        def _sync_wrapper():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                return loop.run_until_complete(func(*args, **kwargs))
            finally:
                loop.close()
                asyncio.set_event_loop(None)
        return benchmark(_sync_wrapper)
    return _wrapper


async def create_and_benchmark(client, runs_data):
    """Helper to create runs and return timing + run_ids."""
    start_time = time.time()
    
    serialized_json = orjson.dumps(runs_data)
    response = await client.post(
        "/runs", 
        content=serialized_json, 
        headers={"Content-Type": "application/json"}
    )
    
    creation_time = time.time() - start_time
    
    assert response.status_code == 201
    run_ids = response.json()["run_ids"]
    
    return creation_time, run_ids


async def get_single_runs_benchmark(client, run_ids):
    """Benchmark individual GET requests."""
    start_time = time.time()
    
    for run_id in run_ids[:10]:  # Test first 10
        response = await client.get(f"/runs/{run_id}")
        assert response.status_code == 200
    
    return time.time() - start_time


async def get_batch_runs_benchmark(client, run_ids):
    """Benchmark batch GET requests."""
    start_time = time.time()
    
    batch_response = await client.post(
        "/runs/batch",
        json=run_ids[:10]  # Test first 10
    )
    
    retrieval_time = time.time() - start_time
    assert batch_response.status_code == 200
    
    return retrieval_time


def test_advanced_streaming_1000_medium(client, aio_benchmark):
    """Test advanced streaming with 1000 medium complexity runs."""
    runs_data = [generate_advanced_run(20, "medium") for _ in range(1000)]
    serialized_json = orjson.dumps(runs_data)

    async def make_request():
        return await client.post(
            "/runs", 
            content=serialized_json, 
            headers={"Content-Type": "application/json"}
        )

    result = aio_benchmark(make_request)
    assert result.status_code == 201
    assert len(result.json()["run_ids"]) == 1000


def test_advanced_complex_structures_200(client, aio_benchmark):
    """Test complex nested structures with 200 runs."""
    runs_data = [generate_advanced_run(50, "complex") for _ in range(200)]
    serialized_json = orjson.dumps(runs_data)

    async def make_request():
        return await client.post(
            "/runs", 
            content=serialized_json, 
            headers={"Content-Type": "application/json"}
        )

    result = aio_benchmark(make_request)
    assert result.status_code == 201
    assert len(result.json()["run_ids"]) == 200


def test_cache_performance_benefit(client, aio_benchmark):
    """Test caching performance benefit on repeated access."""
    # Create some runs first
    runs_data = [generate_advanced_run(30, "medium") for _ in range(50)]
    
    async def setup_and_test_cache():
        # Create runs
        creation_time, run_ids = await create_and_benchmark(client, runs_data)
        
        # First access (cold cache)
        first_access_time = await get_single_runs_benchmark(client, run_ids)
        
        # Second access (warm cache)
        second_access_time = await get_single_runs_benchmark(client, run_ids)
        
        # Cache should make second access faster
        cache_improvement = first_access_time / second_access_time if second_access_time > 0 else 1
        
        return {
            "creation_time": creation_time,
            "first_access": first_access_time,
            "second_access": second_access_time,
            "cache_speedup": cache_improvement
        }
    
    result = aio_benchmark(setup_and_test_cache)
    
    # Verify cache provides benefit (at least 20% improvement)
    assert result["cache_speedup"] > 1.2


def test_batch_vs_individual_retrieval(client, aio_benchmark):
    """Compare batch retrieval vs individual requests."""
    runs_data = [generate_advanced_run(25, "medium") for _ in range(20)]
    
    async def compare_retrieval_methods():
        # Create runs
        creation_time, run_ids = await create_and_benchmark(client, runs_data)
        
        # Individual requests
        individual_time = await get_single_runs_benchmark(client, run_ids)
        
        # Batch request
        batch_time = await get_batch_runs_benchmark(client, run_ids)
        
        batch_improvement = individual_time / batch_time if batch_time > 0 else 1
        
        return {
            "individual_time": individual_time,
            "batch_time": batch_time,
            "batch_speedup": batch_improvement
        }
    
    result = aio_benchmark(compare_retrieval_methods)
    
    # Batch should be significantly faster (at least 3x)
    assert result["batch_speedup"] > 3.0


def test_compression_efficiency(client, aio_benchmark):
    """Test response compression efficiency."""
    runs_data = [generate_advanced_run(100, "complex") for _ in range(10)]
    
    async def test_compression():
        creation_time, run_ids = await create_and_benchmark(client, runs_data)
        
        # Get a run and check if response is compressed
        response = await client.get(f"/runs/{run_ids[0]}")
        assert response.status_code == 200
        
        # Check for compression header
        content_encoding = response.headers.get("content-encoding")
        is_compressed = content_encoding == "gzip"
        
        # Calculate rough compression ratio by response size
        content_length = len(response.content)
        
        return {
            "is_compressed": is_compressed,
            "content_length": content_length,
            "creation_time": creation_time
        }
    
    result = aio_benchmark(test_compression)
    
    # Verify compression is working
    assert result["is_compressed"]


def test_mixed_workload_stress(client, aio_benchmark):
    """Test mixed workload with varying sizes and complexities."""
    
    async def mixed_workload():
        # Create mixed batches
        batch1 = [generate_advanced_run(10, "simple") for _ in range(100)]
        batch2 = [generate_advanced_run(50, "medium") for _ in range(50)]
        batch3 = [generate_advanced_run(200, "complex") for _ in range(20)]
        
        all_run_ids = []
        
        # Create all batches
        for batch in [batch1, batch2, batch3]:
            _, run_ids = await create_and_benchmark(client, batch)
            all_run_ids.extend(run_ids)
        
        # Mixed retrieval pattern
        start_time = time.time()
        
        # Individual gets (random selection)
        random_ids = random.sample(all_run_ids, 15)
        for run_id in random_ids:
            response = await client.get(f"/runs/{run_id}")
            assert response.status_code == 200
        
        # Batch get
        batch_ids = random.sample(all_run_ids, 10)
        batch_response = await client.post("/runs/batch", json=batch_ids)
        assert batch_response.status_code == 200
        
        total_retrieval_time = time.time() - start_time
        
        return {
            "total_runs_created": len(all_run_ids),
            "retrieval_time": total_retrieval_time,
            "runs_per_second": 25 / total_retrieval_time if total_retrieval_time > 0 else 0
        }
    
    result = aio_benchmark(mixed_workload)
    
    # Should handle mixed workload efficiently
    assert result["runs_per_second"] > 10  # At least 10 runs/second retrieval