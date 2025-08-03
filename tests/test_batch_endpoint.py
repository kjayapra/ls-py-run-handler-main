import uuid
import pytest
from httpx import AsyncClient

from ls_py_handler.main import app


@pytest.mark.asyncio
async def test_batch_retrieval_endpoint():
    """Test the new batch retrieval endpoint."""
    # Create some runs first
    runs_data = []
    for i in range(5):
        runs_data.append({
            "trace_id": str(uuid.uuid4()),
            "name": f"Batch Test Run {i}",
            "inputs": {"query": f"test query {i}", "index": i},
            "outputs": {"result": f"test result {i}", "processed": True},
            "metadata": {"batch": True, "run_number": i}
        })
    
    async with AsyncClient(app=app, base_url="http://test") as client:
        # Create runs
        create_response = await client.post("/runs", json=runs_data)
        assert create_response.status_code == 201
        run_ids = create_response.json()["run_ids"]
        
        # Test batch retrieval
        batch_response = await client.post("/runs/batch", json=run_ids)
        assert batch_response.status_code == 200
        
        # Check response format
        batch_data = batch_response.json()
        assert "runs" in batch_data
        assert "count" in batch_data
        assert batch_data["count"] == 5
        assert len(batch_data["runs"]) == 5
        
        # Verify data integrity
        for i, run in enumerate(batch_data["runs"]):
            assert run["name"] == f"Batch Test Run {i}"
            assert run["inputs"]["index"] == i
            assert run["outputs"]["processed"] == True
            assert run["metadata"]["run_number"] == i


@pytest.mark.asyncio
async def test_batch_retrieval_with_cache():
    """Test batch retrieval with caching behavior."""
    runs_data = [{
        "trace_id": str(uuid.uuid4()),
        "name": "Cache Test Run",
        "inputs": {"test": "cache"},
        "outputs": {"cached": True},
        "metadata": {"cache_test": True}
    }]
    
    async with AsyncClient(app=app, base_url="http://test") as client:
        # Create run
        create_response = await client.post("/runs", json=runs_data)
        assert create_response.status_code == 201
        run_ids = create_response.json()["run_ids"]
        
        # First individual access (should cache the result)
        individual_response = await client.get(f"/runs/{run_ids[0]}")
        assert individual_response.status_code == 200
        
        # Batch access (should use cached result)
        batch_response = await client.post("/runs/batch", json=run_ids)
        assert batch_response.status_code == 200
        
        batch_data = batch_response.json()
        individual_data = individual_response.json()
        
        # Data should be identical
        assert batch_data["runs"][0]["name"] == individual_data["name"]
        assert batch_data["runs"][0]["inputs"] == individual_data["inputs"]
        assert batch_data["runs"][0]["outputs"] == individual_data["outputs"]
        assert batch_data["runs"][0]["metadata"] == individual_data["metadata"]


@pytest.mark.asyncio
async def test_batch_retrieval_limits():
    """Test batch retrieval limits and error handling."""
    async with AsyncClient(app=app, base_url="http://test") as client:
        # Test empty batch
        empty_response = await client.post("/runs/batch", json=[])
        assert empty_response.status_code == 400
        assert "No run IDs provided" in empty_response.json()["detail"]
        
        # Test too many IDs (over 100 limit)
        too_many_ids = [str(uuid.uuid4()) for _ in range(101)]
        large_response = await client.post("/runs/batch", json=too_many_ids)
        assert large_response.status_code == 400
        assert "Too many run IDs" in large_response.json()["detail"]


@pytest.mark.asyncio
async def test_compression_headers():
    """Test that responses include compression headers."""
    runs_data = [{
        "trace_id": str(uuid.uuid4()),
        "name": "Compression Test",
        "inputs": {"large_data": "x" * 1000},
        "outputs": {"processed_data": "y" * 1000},
        "metadata": {"compression": True}
    }]
    
    async with AsyncClient(app=app, base_url="http://test") as client:
        # Create run
        create_response = await client.post("/runs", json=runs_data)
        assert create_response.status_code == 201
        run_ids = create_response.json()["run_ids"]
        
        # Test individual endpoint compression
        individual_response = await client.get(f"/runs/{run_ids[0]}")
        assert individual_response.status_code == 200
        assert individual_response.headers.get("content-encoding") == "gzip"
        
        # Test batch endpoint compression
        batch_response = await client.post("/runs/batch", json=run_ids)
        assert batch_response.status_code == 200
        assert batch_response.headers.get("content-encoding") == "gzip"