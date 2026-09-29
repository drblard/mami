"""Single production worker-resource manifest used by deployment and packaging."""
WORKER_RESOURCES = (
    'search_worker.py', 'packed_search_worker.py', 'search_logic.py', 'model_config.py', 'sampling.py', 'text_encoder.py', 'vector_selection.py', 'packed_vectors.py', 'vector_overlay.py', 'vector_sync.py',
    'index_worker.py', 'index_queue.py', 'index_store.py', 'preview_pipeline.py', 'preview_atlas.py', 'preview_retention.py', 'preview_cache_worker.py', 'index_backend.py', 'metadata.py',
    'import_media.py', 'gpu_activity.py', 'photos_batch.py', 'user_store.py', 'search_store.py', 'search_sync.py',
)
