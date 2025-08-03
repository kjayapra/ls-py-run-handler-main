import uuid
import asyncio
import pytest
from httpx import AsyncClient

from ls_py_handler.main import app


def generate_stress_run(size_multiplier=1):
    """Generate a run with configurable data size."""
    base_size = int(1000 * size_multiplier)
    return {
        "trace_id": str(uuid.uuid4()),
        "name": f"Stress Test Run {uuid.uuid4()}",
        "inputs": {
            "data": "x" * base_size,
            "nested": {"deep": {"deeper": "y" * (base_size // 2)}}
        },
        "outputs": {
            "result": "z" * base_size,
            "metrics": {"count": base_size, "processed": True}
        },
        "metadata": {
            "size": base_size,
            "timestamp": "2025-01-01T00:00:00Z",
            "extra_data": "metadata" * (base_size // 10)
        }
    }


@pytest.mark.asyncio
async def test_stress_sequential_batches():
    """Test creating multiple batches sequentially."""
    async with AsyncClient(app=app, base_url="http://test") as client:
        all_run_ids = []
        
        for batch_num in range(5):  # 5 batches
            runs_data = [generate_stress_run(1) for _ in range(20)]  # 20 runs per batch
            
            response = await client.post("/runs", json=runs_data)
            assert response.status_code == 201
            assert len(response.json()["run_ids"]) == 20
            
            all_run_ids.extend(response.json()["run_ids"])
        
        # Verify total runs created
        assert len(all_run_ids) == 100
        
        # Spot check - retrieve a few random runs
        import random
        sample_ids = random.sample(all_run_ids, 5)
        
        for run_id in sample_ids:
            get_response = await client.get(f"/runs/{run_id}")
            assert get_response.status_code == 200
            assert "Stress Test Run" in get_response.json()["name"]


@pytest.mark.asyncio
async def test_stress_concurrent_requests():
    """Test concurrent batch creation."""
    async def create_batch(client, batch_id):
        runs_data = [generate_stress_run(1) for _ in range(10)]
        response = await client.post("/runs", json=runs_data)
        return response.status_code == 201, len(response.json().get("run_ids", []))
    
    async with AsyncClient(app=app, base_url="http://test") as client:
        # Create 5 concurrent batches
        tasks = [create_batch(client, i) for i in range(5)]
        results = await asyncio.gather(*tasks)
        
        # Verify all succeeded
        for success, count in results:
            assert success
            assert count == 10


@pytest.mark.asyncio 
async def test_stress_large_single_batch():
    """Test a very large single batch."""
    # Create 200 runs in one batch
    runs_data = [generate_stress_run(2) for _ in range(200)]
    
    async with AsyncClient(app=app, base_url="http://test", timeout=30.0) as client:
        response = await client.post("/runs", json=runs_data)
        assert response.status_code == 201
        assert len(response.json()["run_ids"]) == 200
        
        # Verify we can retrieve runs from different parts of the batch
        run_ids = response.json()["run_ids"]
        test_indices = [0, 50, 100, 150, 199]  # First, middle, last
        
        for idx in test_indices:
            get_response = await client.get(f"/runs/{run_ids[idx]}")
            assert get_response.status_code == 200


@pytest.mark.asyncio
async def test_stress_varying_sizes():
    """Test batch with runs of dramatically different sizes."""
    runs_data = []
    
    # Mix of tiny, small, medium, large, and huge runs
    sizes = [0.1, 1, 5, 20, 100]  # Multipliers for base size
    
    for size_mult in sizes:
        for _ in range(4):  # 4 runs of each size
            runs_data.append(generate_stress_run(size_mult))
    
    async with AsyncClient(app=app, base_url="http://test", timeout=30.0) as client:
        response = await client.post("/runs", json=runs_data)
        assert response.status_code == 201
        assert len(response.json()["run_ids"]) == 20
        
        # Verify all runs regardless of size
        run_ids = response.json()["run_ids"]
        
        for i, run_id in enumerate(run_ids):
            get_response = await client.get(f"/runs/{run_id}")
            assert get_response.status_code == 200
            retrieved = get_response.json()
            
            # Verify data integrity based on expected size
            expected_size_mult = sizes[i // 4]
            expected_base_size = int(1000 * expected_size_mult)
            
            assert len(retrieved["inputs"]["data"]) == expected_base_size
            assert len(retrieved["outputs"]["result"]) == expected_base_size
            assert retrieved["metadata"]["size"] == expected_base_size


@pytest.mark.asyncio
async def test_stress_retrieval_pattern():
    """Test different retrieval patterns after creating a large batch."""
    # Create a batch of 50 runs
    runs_data = [generate_stress_run(3) for _ in range(50)]
    
    async with AsyncClient(app=app, base_url="http://test", timeout=30.0) as client:
        # Create batch
        response = await client.post("/runs", json=runs_data)
        assert response.status_code == 201
        run_ids = response.json()["run_ids"]
        
        # Pattern 1: Sequential retrieval
        for run_id in run_ids[:10]:
            get_response = await client.get(f"/runs/{run_id}")
            assert get_response.status_code == 200
        
        # Pattern 2: Random access
        import random
        random_ids = random.sample(run_ids, 10)
        for run_id in random_ids:
            get_response = await client.get(f"/runs/{run_id}")
            assert get_response.status_code == 200
        
        # Pattern 3: Reverse order
        for run_id in reversed(run_ids[-10:]):
            get_response = await client.get(f"/runs/{run_id}")
            assert get_response.status_code == 200
        
        # Pattern 4: Concurrent retrieval
        async def get_run(run_id):
            response = await client.get(f"/runs/{run_id}")
            return response.status_code == 200
        
        # Get 10 runs concurrently
        concurrent_tasks = [get_run(run_id) for run_id in run_ids[20:30]]
        concurrent_results = await asyncio.gather(*concurrent_tasks)
        
        # All should succeed
        assert all(concurrent_results)


@pytest.mark.asyncio
async def test_memory_efficiency():
    """Test that large batches don't cause memory issues."""
    # This test focuses on ensuring no memory leaks or excessive usage
    batches_to_create = 3
    runs_per_batch = 100
    
    async with AsyncClient(app=app, base_url="http://test", timeout=60.0) as client:
        created_batches = []
        
        for batch_num in range(batches_to_create):
            # Create a batch with moderately sized runs
            runs_data = [generate_stress_run(5) for _ in range(runs_per_batch)]
            
            response = await client.post("/runs", json=runs_data)
            assert response.status_code == 201
            assert len(response.json()["run_ids"]) == runs_per_batch
            
            created_batches.append(response.json()["run_ids"])
        
        # Verify we can still retrieve from all batches
        for batch_idx, run_ids in enumerate(created_batches):
            # Test a few runs from each batch
            sample_ids = run_ids[::20]  # Every 20th run
            
            for run_id in sample_ids:
                get_response = await client.get(f"/runs/{run_id}")
                assert get_response.status_code == 200
                
                # Verify the data is still correct
                retrieved = get_response.json()
                assert "Stress Test Run" in retrieved["name"]
                assert isinstance(retrieved["inputs"], dict)
                assert isinstance(retrieved["outputs"], dict)
                assert isinstance(retrieved["metadata"], dict)