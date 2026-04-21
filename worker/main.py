import base64
import json
import os
import io
import concurrent.futures # NEW IMPORT
from flask import Flask, request
from google.cloud import storage, firestore
import numpy as np
from numba import njit
from PIL import Image

app = Flask(__name__)
storage_client = storage.Client()
firestore_client = firestore.Client(database="wfc-db")

# ... [KEEP YOUR extract_patterns_and_rules FUNCTION] ...

# NEW: Add cache=True so the child process doesn't recompile every time
@njit(cache=True) 
def execute_wfc(grid_size, num_patterns, rules, weights):
    # ... [KEEP YOUR ENTIRE NUMBA FUNCTION EXACTLY AS IS] ...

# NEW HELPER FUNCTION: Wraps the retry loop so it can be sent to the executor
def solve_wfc_with_retries(grid_size, num_patterns, rules, weights):
    attempts = 0
    while True:
        attempts += 1
        print(f"Executing WFC (Attempt {attempts})...")
        result_grid = execute_wfc(grid_size, num_patterns, rules, weights)
        if result_grid.shape != (1, 1):
            return result_grid

@app.route('/', methods=['POST'])
def pubsub_push():
    envelope = request.get_json()
    if not envelope or 'message' not in envelope:
        return 'Bad Request', 400

    msg_data = base64.b64decode(envelope['message']['data']).decode('utf-8')
    work_order = json.loads(msg_data)

    input_bucket = work_order.get('input_bucket')
    input_file = work_order.get('input_filename')
    output_bucket = work_order.get('output_bucket')
    job_id = work_order.get('job_id')
    patch_size = int(work_order.get('patch_size', 3))
    grid_size = int(work_order.get('output_size', 128))

    if not all([input_bucket, input_file, output_bucket, job_id]):
        return 'Missing required info in payload', 400
    
    print(f"Processing Job: {job_id} | Output: {grid_size}x{grid_size} | Patch: {patch_size}")

    try:
        in_blob = storage_client.bucket(input_bucket).blob(input_file)
        img_bytes = in_blob.download_as_bytes()
        seed_img = Image.open(io.BytesIO(img_bytes)).convert('RGB')
        seed_array = np.array(seed_img)
        
        patterns, weights, rules = extract_patterns_and_rules(seed_array, N=patch_size)
        num_patterns = len(patterns)
        
        # -------------------------------------------------------------
        # NEW: The 5-Minute Circuit Breaker
        # -------------------------------------------------------------
        with concurrent.futures.ProcessPoolExecutor(max_workers=1) as executor:
            future = executor.submit(solve_wfc_with_retries, grid_size, num_patterns, rules, weights)
            try:
                # Wait a maximum of 300 seconds (5 minutes)
                result_grid = future.result(timeout=300) 
            except concurrent.futures.TimeoutError:
                print(f"Job {job_id} hit the 5-minute limit and was aborted.")
                # Update the database so the frontend knows
                firestore_client.collection("wfc_jobs").document(job_id).update({"status": "TIMED OUT"})
                # CRITICAL: Return 200 so Pub/Sub deletes the message and doesn't retry
                return 'Timeout', 200
        # -------------------------------------------------------------
                
        final_array = np.zeros((grid_size, grid_size, 3), dtype=np.uint8)
        for y in range(grid_size):
            for x in range(grid_size):
                pattern_idx = result_grid[y, x]
                final_array[y, x] = patterns[pattern_idx][0, 0]
                
        final_img = Image.fromarray(final_array, 'RGB')
        out_io = io.BytesIO()
        final_img.save(out_io, format='PNG')
        
        out_name = f"generated-{job_id}.png"
        out_blob = storage_client.bucket(output_bucket).blob(out_name)
        out_blob.upload_from_string(out_io.getvalue(), content_type='image/png')
        
        public_url = f"https://storage.googleapis.com/{output_bucket}/{out_name}"

        firestore_client.collection("wfc_jobs").document(job_id).update({
            "status": "COMPLETE",
            "output_url": public_url
        })
        
        print(f"Job {job_id} complete. Saved to {public_url}")
        return 'Success', 200

    except Exception as e:
        print(f"Pipeline Error for Job {job_id}: {e}")
        firestore_client.collection("wfc_jobs").document(job_id).update({"status": "ERROR"})
        return 'Internal Server Error', 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)