import uuid
from typing import Any, Dict, List, Optional

import asyncpg
import orjson
from aiobotocore.session import get_session
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response
from pydantic import UUID4, BaseModel, Field
import gzip

from ls_py_handler.config.settings import settings
from ls_py_handler.cache import get_cache

router = APIRouter(prefix="/runs", tags=["runs"])


def build_batch_with_streaming_json(
    runs: List["Run"],
) -> tuple[bytes, List[Dict[str, Any]]]:
    """
    Build JSON batch with streaming approach for better memory efficiency.
    
    This approach builds JSON incrementally while tracking field positions,
    avoiding the need to search through the entire batch.
    """
    if not runs:
        return b"[]", []

    # Pre-calculate run data and field JSON
    run_data_list = []
    for run in runs:
        run_dict = run.model_dump()
        field_jsons = {}
        
        for field in ["inputs", "outputs", "metadata"]:
            field_value = run_dict.get(field, {})
            field_jsons[field] = orjson.dumps(field_value)
        
        run_data_list.append({
            "run_dict": run_dict,
            "field_jsons": field_jsons
        })

    # Build JSON batch incrementally with position tracking
    batch_parts = [b'[']
    current_position = 1  # Start after '['
    field_references = []

    for i, run_data in enumerate(run_data_list):
        if i > 0:
            batch_parts.append(b',')
            current_position += 1

        # Serialize the entire run
        run_json = orjson.dumps(run_data["run_dict"])
        
        # Calculate field positions within this run
        field_refs = {}
        run_start_pos = current_position
        
        for field in ["inputs", "outputs", "metadata"]:
            field_json = run_data["field_jsons"][field]
            
            # Find field position within the run JSON
            field_pos_in_run = run_json.find(field_json)
            
            if field_pos_in_run != -1:
                field_start = run_start_pos + field_pos_in_run
                field_end = field_start + len(field_json)
                field_refs[field] = f"s3://{settings.S3_BUCKET_NAME}/{{object_key}}#{field_start}:{field_end}/{field}"
            else:
                field_refs[field] = ""
        
        field_references.append(field_refs)
        
        # Add run JSON to batch
        batch_parts.append(run_json)
        current_position += len(run_json)

    batch_parts.append(b']')
    
    # Combine all parts
    batch_data = b''.join(batch_parts)
    
    return batch_data, field_references


def extract_field_positions_optimized(
    run_json: bytes, base_offset: int
) -> Dict[str, Dict[str, int]]:
    """
    Extract field positions from a single run's JSON.
    This is a more accurate approach that parses JSON structure.
    """
    try:
        # Parse the run to get field values
        run_dict = orjson.loads(run_json)
        field_positions = {}

        for field in ["inputs", "outputs", "metadata"]:
            if field in run_dict:
                field_value = run_dict[field]
                field_json = orjson.dumps(field_value)

                # Find the field within this specific run's JSON
                field_start = run_json.find(field_json)
                if field_start != -1:
                    field_positions[field] = {
                        "start": base_offset + field_start,
                        "end": base_offset + field_start + len(field_json),
                    }

        return field_positions
    except Exception:
        return {}


class RunRequest(BaseModel):
    """Request model for creating runs - id is always server-generated."""
    trace_id: UUID4
    name: str
    inputs: Dict[str, Any] = {}
    outputs: Dict[str, Any] = {}
    metadata: Dict[str, Any] = {}


class Run(BaseModel):
    """Internal run model with server-generated id."""
    id: UUID4 = Field(default_factory=uuid.uuid4)
    trace_id: UUID4
    name: str
    inputs: Dict[str, Any] = {}
    outputs: Dict[str, Any] = {}
    metadata: Dict[str, Any] = {}


async def get_db_conn():
    """Get a database connection."""
    conn = await asyncpg.connect(
        user=settings.DB_USER,
        password=settings.DB_PASSWORD,
        database=settings.DB_NAME,
        host=settings.DB_HOST,
        port=settings.DB_PORT,
    )
    try:
        yield conn
    finally:
        await conn.close()


async def get_s3_client():
    """Get an S3 client for MinIO."""
    session = get_session()
    async with session.create_client(
        "s3",
        endpoint_url=settings.S3_ENDPOINT_URL,
        aws_access_key_id=settings.S3_ACCESS_KEY,
        aws_secret_access_key=settings.S3_SECRET_KEY,
        region_name=settings.S3_REGION,
    ) as client:
        yield client


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_runs(
    run_requests: List[RunRequest],
    db: asyncpg.Connection = Depends(get_db_conn),
    s3: Any = Depends(get_s3_client),
):
    """
    Create new runs in batch - OPTIMIZED VERSION.

    Takes a JSON array of RunRequest objects, uploads them to MinIO,
    and stores references to certain fields in PostgreSQL.
    """
    if not run_requests:
        raise HTTPException(status_code=400, detail="No runs provided")

    # Convert RunRequest objects to Run objects with server-generated IDs
    runs = [
        Run(
            trace_id=req.trace_id,
            name=req.name,
            inputs=req.inputs,
            outputs=req.outputs,
            metadata=req.metadata,
        )
        for req in run_requests
    ]

    # Generate batch ID
    batch_id = str(uuid.uuid4())
    object_key = f"batches/{batch_id}.json"

    # OPTIMIZATION 1: Use streaming JSON approach for better memory efficiency
    batch_data, field_references = build_batch_with_streaming_json(runs)

    # Upload the batch data to S3
    await s3.put_object(
        Bucket=settings.S3_BUCKET_NAME,
        Key=object_key,
        Body=batch_data,
        ContentType="application/json",
    )

    # OPTIMIZATION 2: Update field references with actual object key
    for field_refs in field_references:
        for field in ["inputs", "outputs", "metadata"]:
            if field_refs[field]:
                field_refs[field] = field_refs[field].format(object_key=object_key)

    # OPTIMIZATION 3: Use batch database insert with size tracking
    insert_data = [
        (
            run.id,
            run.trace_id,
            run.name,
            field_refs["inputs"],
            field_refs["outputs"],
            field_refs["metadata"],
            len(batch_data),  # total_size for cache optimization
        )
        for run, field_refs in zip(runs, field_references, strict=False)
    ]

    # Single batch insert operation with size tracking
    await db.executemany(
        """
        INSERT INTO runs (id, trace_id, name, inputs, outputs, metadata, total_size)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        """,
        insert_data,
    )

    # Return the run IDs
    inserted_ids = [str(run.id) for run in runs]
    return {"status": "created", "run_ids": inserted_ids}


@router.get("/{run_id}", status_code=status.HTTP_200_OK)
async def get_run(
    run_id: UUID4,
    db: asyncpg.Connection = Depends(get_db_conn),
    s3: Any = Depends(get_s3_client),
) -> Response:
    """
    Get a run by its ID with caching and compression.
    """
    cache = get_cache()
    cache_key = f"run:{run_id}"
    
    # OPTIMIZATION 5: Check cache first
    cached_result = await cache.get(cache_key)
    if cached_result:
        # Return cached result with compression
        json_data = orjson.dumps(cached_result)
        compressed_data = gzip.compress(json_data)
        return Response(
            content=compressed_data,
            media_type="application/json",
            headers={"Content-Encoding": "gzip"}
        )
    
    # Fetch the run from the database
    row = await db.fetchrow(
        """
        SELECT id, trace_id, name, inputs, outputs, metadata, total_size
        FROM runs
        WHERE id = $1
        """,
        run_id,
    )

    if not row:
        raise HTTPException(status_code=404, detail=f"Run with ID {run_id} not found")

    run_data = dict(row)

    # Function to parse S3 reference
    def parse_s3_ref(ref):
        if not ref or not ref.startswith("s3://"):
            return None, None, None, None

        parts = ref.split("/")
        bucket = parts[2]
        key = "/".join(parts[3:]).split("#")[0]

        if "#" in ref:
            offset_part = ref.split("#")[1]
            if ":" in offset_part and "/" in offset_part:
                offsets, field = offset_part.split("/")
                start_offset, end_offset = map(int, offsets.split(":"))
                return bucket, key, (start_offset, end_offset), field

        return bucket, key, None, None

    # OPTIMIZATION 4: Consolidate S3 requests into a single call when possible
    async def fetch_all_fields_optimized(field_refs):
        """Fetch all fields with minimal S3 requests."""
        # Group references by S3 object
        s3_objects = {}

        for field_name, ref in field_refs.items():
            if ref and ref.startswith("s3://"):
                bucket, key, offsets, field = parse_s3_ref(ref)
                if bucket and key and offsets:
                    if key not in s3_objects:
                        s3_objects[key] = {
                            "bucket": bucket,
                            "fields": {},
                            "min_start": float("inf"),
                            "max_end": 0,
                        }

                    start_offset, end_offset = offsets
                    s3_objects[key]["fields"][field_name] = {
                        "start": start_offset,
                        "end": end_offset,
                        "field": field,
                    }
                    s3_objects[key]["min_start"] = min(
                        s3_objects[key]["min_start"], start_offset
                    )
                    s3_objects[key]["max_end"] = max(
                        s3_objects[key]["max_end"], end_offset
                    )

        results = {"inputs": {}, "outputs": {}, "metadata": {}}

        # Fetch each S3 object with consolidated range
        for key, obj_info in s3_objects.items():
            try:
                # Use consolidated byte range
                start = obj_info["min_start"]
                end = obj_info["max_end"]
                byte_range = f"bytes={start}-{end-1}"

                response = await s3.get_object(
                    Bucket=obj_info["bucket"], Key=key, Range=byte_range
                )
                async with response["Body"] as stream:
                    consolidated_data = await stream.read()

                # Extract individual fields from consolidated data
                for field_name, field_info in obj_info["fields"].items():
                    field_start = field_info["start"] - start
                    field_end = field_info["end"] - start
                    field_data = consolidated_data[field_start:field_end]

                    try:
                        results[field_name] = orjson.loads(field_data)
                    except Exception as parse_error:
                        print(f"Error parsing {field_name}: {parse_error}")
                        results[field_name] = {}

            except Exception as e:
                print(f"Error fetching S3 object {key}: {e}")
                # Fallback to empty results for this object
                for field_name in obj_info["fields"]:
                    results[field_name] = {}

        return results

    # Use the optimized fetching function
    field_refs = {
        "inputs": run_data["inputs"],
        "outputs": run_data["outputs"],
        "metadata": run_data["metadata"],
    }

    field_data = await fetch_all_fields_optimized(field_refs)
    inputs, outputs, metadata = (
        field_data["inputs"],
        field_data["outputs"],
        field_data["metadata"],
    )

    result = {
        "id": str(run_data["id"]),
        "trace_id": str(run_data["trace_id"]),
        "name": run_data["name"],
        "inputs": inputs,
        "outputs": outputs,
        "metadata": metadata,
    }
    
    # OPTIMIZATION 6: Cache the result for future requests
    await cache.set(cache_key, result)
    
    # OPTIMIZATION 7: Return compressed response
    json_data = orjson.dumps(result)
    compressed_data = gzip.compress(json_data)
    return Response(
        content=compressed_data,
        media_type="application/json",
        headers={"Content-Encoding": "gzip"}
    )


@router.post("/batch", status_code=status.HTTP_200_OK)
async def get_runs_batch(
    run_ids: List[UUID4],
    db: asyncpg.Connection = Depends(get_db_conn),
    s3: Any = Depends(get_s3_client),
) -> Response:
    """
    Get multiple runs by their IDs in a single request.
    This is much more efficient than making individual GET requests.
    """
    if not run_ids:
        raise HTTPException(status_code=400, detail="No run IDs provided")
    
    if len(run_ids) > 100:  # Limit batch size
        raise HTTPException(status_code=400, detail="Too many run IDs (max 100)")
    
    cache = get_cache()
    results = {}
    uncached_ids = []
    
    # Check cache for each run
    for run_id in run_ids:
        cache_key = f"run:{run_id}"
        cached_result = await cache.get(cache_key)
        if cached_result:
            results[str(run_id)] = cached_result
        else:
            uncached_ids.append(run_id)
    
    # Fetch uncached runs from database
    if uncached_ids:
        # Single query for all uncached runs
        rows = await db.fetch(
            """
            SELECT id, trace_id, name, inputs, outputs, metadata, total_size
            FROM runs
            WHERE id = ANY($1)
            """,
            uncached_ids,
        )
        
        # Group S3 requests by object for efficiency
        s3_requests = {}
        run_data_map = {}
        
        for row in rows:
            run_data = dict(row)
            run_data_map[str(row["id"])] = run_data
            
            # Parse S3 references and group by object
            for field in ["inputs", "outputs", "metadata"]:
                ref = run_data[field]
                if ref and ref.startswith("s3://"):
                    # Parse S3 reference inline
                    parts = ref.split("/")
                    bucket = parts[2]
                    key = "/".join(parts[3:]).split("#")[0]

                    if "#" in ref:
                        offset_part = ref.split("#")[1]
                        if ":" in offset_part and "/" in offset_part:
                            offsets_str, _ = offset_part.split("/")  # field_name not needed here
                            start_offset, end_offset = map(int, offsets_str.split(":"))
                            offsets = (start_offset, end_offset)
                        else:
                            continue
                    else:
                        continue
                    if bucket and key and offsets:
                        if key not in s3_requests:
                            s3_requests[key] = {
                                'bucket': bucket,
                                'fields': [],
                                'min_start': float('inf'),
                                'max_end': 0
                            }
                        
                        start_offset, end_offset = offsets
                        s3_requests[key]['fields'].append({
                            'run_id': str(row["id"]),
                            'field': field,
                            'start': start_offset,
                            'end': end_offset
                        })
                        s3_requests[key]['min_start'] = min(s3_requests[key]['min_start'], start_offset)
                        s3_requests[key]['max_end'] = max(s3_requests[key]['max_end'], end_offset)
        
        # Fetch S3 data with consolidated requests
        for key, req_info in s3_requests.items():
            try:
                start = req_info['min_start']
                end = req_info['max_end']
                byte_range = f"bytes={start}-{end-1}"
                
                response = await s3.get_object(
                    Bucket=req_info['bucket'],
                    Key=key,
                    Range=byte_range
                )
                async with response["Body"] as stream:
                    consolidated_data = await stream.read()
                
                # Extract individual fields
                for field_info in req_info['fields']:
                    run_id = field_info['run_id']
                    field = field_info['field']
                    field_start = field_info['start'] - start
                    field_end = field_info['end'] - start
                    field_data = consolidated_data[field_start:field_end]
                    
                    try:
                        parsed_data = orjson.loads(field_data)
                        if run_id not in results:
                            results[run_id] = {
                                "id": run_id,
                                "trace_id": str(run_data_map[run_id]["trace_id"]),
                                "name": run_data_map[run_id]["name"],
                                "inputs": {},
                                "outputs": {},
                                "metadata": {}
                            }
                        results[run_id][field] = parsed_data
                    except Exception:
                        # Handle parsing errors gracefully
                        if run_id not in results:
                            results[run_id] = {
                                "id": run_id,
                                "trace_id": str(run_data_map[run_id]["trace_id"]),
                                "name": run_data_map[run_id]["name"],
                                "inputs": {},
                                "outputs": {},
                                "metadata": {}
                            }
                        results[run_id][field] = {}
                        
            except Exception:
                # If S3 fetch fails, return empty data
                for field_info in req_info['fields']:
                    run_id = field_info['run_id']
                    if run_id not in results:
                        results[run_id] = {
                            "id": run_id,
                            "trace_id": str(run_data_map[run_id]["trace_id"]),
                            "name": run_data_map[run_id]["name"],
                            "inputs": {},
                            "outputs": {},
                            "metadata": {}
                        }
        
        # Cache the newly fetched results
        for run_id, result in results.items():
            if run_id in [str(uid) for uid in uncached_ids]:
                cache_key = f"run:{run_id}"
                await cache.set(cache_key, result)
    
    # Return results as list in same order as requested
    ordered_results = []
    for run_id in run_ids:
        run_id_str = str(run_id)
        if run_id_str in results:
            ordered_results.append(results[run_id_str])
        else:
            # Run not found
            ordered_results.append(None)
    
    # Filter out None values and return
    valid_results = [r for r in ordered_results if r is not None]
    
    # Return compressed response
    json_data = orjson.dumps({"runs": valid_results, "count": len(valid_results)})
    compressed_data = gzip.compress(json_data)
    return Response(
        content=compressed_data,
        media_type="application/json",
        headers={"Content-Encoding": "gzip"}
    )
