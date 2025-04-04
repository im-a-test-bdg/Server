from flask import Flask, request, jsonify, send_file, render_template_string
from flask_cors import CORS
import os
import json
import sqlite3
import schedule
import time
import threading
from datetime import datetime
import logging
import subprocess
import nltk
import stat  # Added for permission debugging
from utils.db_helpers import init_db, store_interactions

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler("backdoor_ai.log"), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# Set the base directory for persistent storage
BASE_DIR = os.getenv("RENDER_DISK_PATH", "/opt/render/project")
DB_PATH = os.path.join(BASE_DIR, "data", "interactions.db")
MODEL_DIR = os.path.join(BASE_DIR, "models")

# Create a thread lock for database operations
db_lock = threading.RLock()

# Ensure directories exist
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
os.makedirs(MODEL_DIR, exist_ok=True)

# Debug: Check permissions of the base directory
def check_permissions(path):
    try:
        stats = os.stat(path)
        permissions = stat.filemode(stats.st_mode)
        logger.info(f"Permissions for {path}: {permissions}")
    except Exception as e:
        logger.error(f"Cannot check permissions for {path}: {e}")

check_permissions(BASE_DIR)

# Set NLTK data path to persistent disk
NLTK_DATA_PATH = os.path.join(BASE_DIR, "nltk_data")
os.makedirs(NLTK_DATA_PATH, exist_ok=True)
nltk.data.path.append(NLTK_DATA_PATH)

# Download NLTK data if not present
try:
    for resource in ['punkt', 'stopwords', 'wordnet']:
        resource_path = os.path.join(NLTK_DATA_PATH, resource)
        if not os.path.exists(resource_path):
            logger.info(f"Downloading NLTK resource: {resource} to {NLTK_DATA_PATH}")
            nltk.download(resource, download_dir=NLTK_DATA_PATH, quiet=True)
except Exception as e:
    logger.error(f"Failed to download NLTK data to {NLTK_DATA_PATH}: {e}. Using fallback.")
    # Don't raise the exception, attempt to continue without these resources
    pass

# Initialize Flask app
app = Flask(__name__)
CORS(app)

PORT = int(os.getenv("PORT", 10000))
init_db(DB_PATH)
API_KEY = os.getenv("API_KEY", "rnd_2DfFj1QmKeAWcXF5u9Z0oV35kBiN")

@app.route('/api/ai/learn', methods=['POST'])
def collect_data():
    if request.headers.get('X-API-Key') != API_KEY:
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401
    try:
        data = request.json
        logger.info(f"Received learning data from device: {data.get('deviceId', 'unknown')}")
        if not data or 'interactions' not in data:
            return jsonify({'success': False, 'message': 'Invalid data format'}), 400
        
        # Use thread lock for database operations
        with db_lock:
            store_interactions(DB_PATH, data)
            
        latest_model = get_latest_model_info()
        return jsonify({
            'success': True,
            'message': 'Data received successfully',
            'latestModelVersion': latest_model['version'],
            'modelDownloadURL': f"https://{request.host}/api/ai/models/{latest_model['version']}"
        })
    except Exception as e:
        logger.error(f"Error processing learning data: {str(e)}")
        return jsonify({'success': False, 'message': f'Error: {str(e)}'}), 500

@app.route('/api/ai/upload-model', methods=['POST'])
def upload_model():
    """Endpoint for uploading user-trained .mlmodel files to be combined with server models."""
    if request.headers.get('X-API-Key') != API_KEY:
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401
    
    try:
        # Check if file is included in the request
        if 'model' not in request.files:
            return jsonify({'success': False, 'message': 'No model file provided'}), 400
        
        model_file = request.files['model']
        
        # Check if a valid file was selected
        if model_file.filename == '':
            return jsonify({'success': False, 'message': 'No model file selected'}), 400
        
        # Get device ID and other metadata
        device_id = request.form.get('deviceId', 'unknown')
        app_version = request.form.get('appVersion', 'unknown')
        description = request.form.get('description', '')
        
        # Ensure the file is a CoreML model
        if not model_file.filename.endswith('.mlmodel'):
            return jsonify({'success': False, 'message': 'File must be a CoreML model (.mlmodel)'}), 400
        
        # Create directory for uploaded models if it doesn't exist
        UPLOADED_MODELS_DIR = os.path.join(MODEL_DIR, "uploaded")
        os.makedirs(UPLOADED_MODELS_DIR, exist_ok=True)
        
        # Generate a unique filename
        timestamp = int(datetime.now().timestamp())
        unique_filename = f"model_upload_{device_id}_{timestamp}.mlmodel"
        file_path = os.path.join(UPLOADED_MODELS_DIR, unique_filename)
        
        # Save the uploaded model
        model_file.save(file_path)
        logger.info(f"Saved uploaded model from device {device_id} to {file_path}")
        
        # Store model metadata in database
        from utils.db_helpers import store_uploaded_model
        with db_lock:
            model_id = store_uploaded_model(
                DB_PATH, 
                device_id=device_id,
                app_version=app_version,
                description=description,
                file_path=file_path,
                file_size=os.path.getsize(file_path),
                original_filename=model_file.filename
            )
        
        # Trigger async model retraining if enough models are uploaded
        from learning.trainer import should_retrain, trigger_retraining
        if should_retrain(DB_PATH):
            threading.Thread(target=trigger_retraining, args=(DB_PATH,), daemon=True).start()
            retraining_status = "Model retraining triggered"
        else:
            retraining_status = "Model will be incorporated in next scheduled training"
        
        # Return success response
        latest_model = get_latest_model_info()
        return jsonify({
            'success': True,
            'message': f'Model uploaded successfully. {retraining_status}',
            'modelId': model_id,
            'latestModelVersion': latest_model['version'],
            'modelDownloadURL': f"https://{request.host}/api/ai/models/{latest_model['version']}"
        })
        
    except Exception as e:
        logger.error(f"Error uploading model: {str(e)}")
        return jsonify({'success': False, 'message': f'Error: {str(e)}'}), 500

@app.route('/api/ai/models/<version>', methods=['GET'])
def get_model(version):
    if request.headers.get('X-API-Key') != API_KEY:
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401
    
    model_path = os.path.join(MODEL_DIR, f"model_{version}.mlmodel")
    if os.path.exists(model_path):
        logger.info(f"Serving model version {version}")
        try:
            return send_file(model_path, mimetype='application/octet-stream')
        except Exception as e:
            logger.error(f"Error sending model file: {str(e)}")
            return jsonify({'success': False, 'message': f'Error retrieving model: {str(e)}'}), 500
    else:
        logger.warning(f"Model version {version} not found")
        return jsonify({'success': False, 'message': 'Model not found'}), 404

@app.route('/api/ai/latest-model', methods=['GET'])
def latest_model():
    if request.headers.get('X-API-Key') != API_KEY:
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401
    model_info = get_latest_model_info()
    return jsonify({
        'success': True,
        'message': 'Latest model info',
        'latestModelVersion': model_info['version'],
        'modelDownloadURL': f"https://{request.host}/api/ai/models/{model_info['version']}"
    })

@app.route('/api/ai/stats', methods=['GET'])
def get_stats():
    admin_key = os.getenv("ADMIN_API_KEY", "rnd_2DfFj1QmKeAWcXF5u9Z0oV35kBiN")
    if request.headers.get('X-Admin-Key') != admin_key:
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401
    
    conn = None
    try:
        with db_lock:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM interactions")
            total_interactions = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(DISTINCT device_id) FROM interactions")
            unique_devices = cursor.fetchone()[0]
            cursor.execute("SELECT AVG(rating) FROM feedback")
            avg_rating = cursor.fetchone()[0] or 0
            cursor.execute("""
                SELECT detected_intent, COUNT(*) as count 
                FROM interactions 
                GROUP BY detected_intent 
                ORDER BY count DESC 
                LIMIT 5
            """)
            top_intents = [{"intent": row[0], "count": row[1]} for row in cursor.fetchall()]
        
        model_info = get_latest_model_info()
        return jsonify({
            'success': True,
            'stats': {
                'totalInteractions': total_interactions,
                'uniqueDevices': unique_devices,
                'averageFeedbackRating': round(avg_rating, 2),
                'topIntents': top_intents,
                'latestModelVersion': model_info['version'],
                'lastTrainingDate': model_info.get('training_date', 'Unknown')
            }
        })
    except Exception as e:
        logger.error(f"Error getting stats: {str(e)}")
        return jsonify({'success': False, 'message': f'Error: {str(e)}'}), 500
    finally:
        if conn:
            conn.close()

def get_latest_model_info():
    info_path = os.path.join(MODEL_DIR, "latest_model.json")
    try:
        if not os.path.exists(info_path):
            default_info = {
                'version': '1.0.0',
                'path': os.path.join(MODEL_DIR, 'model_1.0.0.mlmodel'),
                'training_date': datetime.now().isoformat()
            }
            # Ensure the directory exists
            os.makedirs(os.path.dirname(info_path), exist_ok=True)
            with open(info_path, 'w') as f:
                json.dump(default_info, f)
            return default_info
            
        with open(info_path, 'r') as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Error accessing model info: {e}")
        # Return a fallback if file access fails
        return {
            'version': '1.0.0',
            'path': os.path.join(MODEL_DIR, 'model_1.0.0.mlmodel'),
            'training_date': datetime.now().isoformat()
        }

def train_model_job():
    # Lazy import to avoid circular import
    from learning.trainer import train_new_model
    try:
        logger.info("Starting scheduled model training")
        with db_lock:
            new_version = train_new_model(DB_PATH)
        logger.info(f"Model training completed. New version: {new_version}")
    except Exception as e:
        logger.error(f"Model training failed: {str(e)}")

def run_scheduler():
    schedule.every().day.at("02:00").do(train_model_job)
    while True:
        schedule.run_pending()
        time.sleep(60)

scheduler_thread = threading.Thread(target=run_scheduler)
scheduler_thread.daemon = True
scheduler_thread.start()

# Add a basic health check endpoint
@app.route('/health', methods=['GET'])
def health_check():
    try:
        # Check if database is accessible
        conn = None
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.execute("SELECT 1")
            db_status = "healthy"
        except Exception as e:
            db_status = f"unhealthy: {str(e)}"
        finally:
            if conn:
                conn.close()
                
        # Check if model directory is accessible    
        model_status = "healthy" if os.access(MODEL_DIR, os.R_OK | os.W_OK) else "unhealthy: permission denied"
        
        return jsonify({
            'status': 'up',
            'database': db_status,
            'models': model_status,
            'timestamp': datetime.now().isoformat()
        })
    except Exception as e:
        return jsonify({
            'status': 'error',
            'message': str(e),
            'timestamp': datetime.now().isoformat()
        }), 500

# API Documentation page
@app.route('/', methods=['GET'])
def api_documentation():
    # HTML template with API docs and copy feature
    html_template = '''
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Backdoor AI API Documentation</title>
        <style>
            :root {
                --primary-color: #2563eb;
                --primary-hover: #1e40af;
                --secondary-color: #64748b;
                --bg-color: #f8fafc;
                --card-bg: #ffffff;
                --code-bg: #f1f5f9;
                --border-color: #e2e8f0;
                --text-color: #334155;
            }
            
            body {
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
                line-height: 1.6;
                color: var(--text-color);
                background-color: var(--bg-color);
                margin: 0;
                padding: 20px;
            }
            
            .container {
                max-width: 1000px;
                margin: 0 auto;
            }
            
            header {
                margin-bottom: 40px;
                text-align: center;
                padding-bottom: 20px;
                border-bottom: 1px solid var(--border-color);
            }
            
            h1 {
                color: var(--primary-color);
                margin-bottom: 10px;
            }
            
            h2 {
                margin-top: 40px;
                padding-bottom: 10px;
                border-bottom: 1px solid var(--border-color);
            }
            
            h3 {
                margin-top: 25px;
                color: var(--secondary-color);
            }
            
            .endpoint-card {
                background-color: var(--card-bg);
                border-radius: 8px;
                box-shadow: 0 2px 10px rgba(0, 0, 0, 0.05);
                padding: 20px;
                margin-bottom: 30px;
                position: relative;
            }
            
            .method {
                display: inline-block;
                padding: 4px 8px;
                border-radius: 4px;
                font-weight: bold;
                font-size: 14px;
                color: white;
                margin-right: 10px;
            }
            
            .get {
                background-color: #22c55e;
            }
            
            .post {
                background-color: #3b82f6;
            }
            
            .path {
                font-family: monospace;
                font-size: 18px;
                font-weight: 600;
                vertical-align: middle;
            }
            
            .copy-btn {
                position: absolute;
                top: 20px;
                right: 20px;
                background-color: var(--primary-color);
                color: white;
                border: none;
                border-radius: 4px;
                padding: 8px 12px;
                cursor: pointer;
                font-size: 14px;
                transition: background-color 0.2s;
            }
            
            .copy-btn:hover {
                background-color: var(--primary-hover);
            }
            
            pre {
                background-color: var(--code-bg);
                padding: 15px;
                border-radius: 6px;
                overflow: auto;
                font-family: monospace;
                font-size: 14px;
            }
            
            code {
                font-family: monospace;
                background-color: var(--code-bg);
                padding: 2px 5px;
                border-radius: 4px;
                font-size: 14px;
            }
            
            .description {
                margin: 15px 0;
            }
            
            .auth-info {
                margin-top: 15px;
                padding: 10px;
                background-color: #fffbeb;
                border-left: 4px solid #f59e0b;
                border-radius: 4px;
            }
            
            .request-example, .response-example {
                margin-top: 15px;
            }
            
            .parameters {
                margin-top: 15px;
            }
            
            table {
                width: 100%;
                border-collapse: collapse;
                margin-top: 10px;
            }
            
            th, td {
                text-align: left;
                padding: 12px;
                border-bottom: 1px solid var(--border-color);
            }
            
            th {
                background-color: var(--code-bg);
                font-weight: 600;
            }
            
            footer {
                margin-top: 60px;
                text-align: center;
                padding-top: 20px;
                border-top: 1px solid var(--border-color);
                color: var(--secondary-color);
                font-size: 14px;
            }
            
            .tooltip {
                position: relative;
                display: inline-block;
            }
            
            .tooltip .tooltiptext {
                visibility: hidden;
                width: 140px;
                background-color: #555;
                color: #fff;
                text-align: center;
                border-radius: 6px;
                padding: 5px;
                position: absolute;
                z-index: 1;
                bottom: 150%;
                left: 50%;
                margin-left: -75px;
                opacity: 0;
                transition: opacity 0.3s;
            }
            
            .tooltip .tooltiptext::after {
                content: "";
                position: absolute;
                top: 100%;
                left: 50%;
                margin-left: -5px;
                border-width: 5px;
                border-style: solid;
                border-color: #555 transparent transparent transparent;
            }
        </style>
    </head>
    <body>
        <div class="container">
            <header>
                <h1>Backdoor AI API Documentation</h1>
                <p>API documentation for the Backdoor AI Learning Server</p>
            </header>
            
            <h2>Endpoints</h2>
            
            <!-- POST /api/ai/learn -->
            <div class="endpoint-card">
                <span class="method post">POST</span>
                <span class="path">/api/ai/learn</span>
                <button class="copy-btn" onclick="copyToClipboard('https://' + window.location.host + '/api/ai/learn')">Copy URL</button>
                
                <div class="description">
                    <p>Submit interaction data from devices to be used for model training. Returns information about the latest model version.</p>
                </div>
                
                <div class="auth-info">
                    <strong>Authentication Required:</strong> Header <code>X-API-Key</code> must be provided.
                </div>
                
                <div class="request-example">
                    <h3>Request Example</h3>
                    <pre>{
  "deviceId": "device_123",
  "appVersion": "1.2.0",
  "modelVersion": "1.0.0",
  "osVersion": "iOS 15.0",
  "interactions": [
    {
      "id": "int_abc123",
      "timestamp": "2023-06-15T14:30:00Z",
      "userMessage": "Turn on the lights",
      "aiResponse": "Turning on the lights",
      "detectedIntent": "light_on",
      "confidenceScore": 0.92,
      "feedback": {
        "rating": 5,
        "comment": "Perfect response"
      }
    }
  ]
}</pre>
                </div>
                
                <div class="response-example">
                    <h3>Response Example</h3>
                    <pre>{
  "success": true,
  "message": "Data received successfully",
  "latestModelVersion": "1.0.1712052481",
  "modelDownloadURL": "https://yourdomain.com/api/ai/models/1.0.1712052481"
}</pre>
                </div>
            </div>
            
            <!-- POST /api/ai/upload-model -->
            <div class="endpoint-card">
                <span class="method post">POST</span>
                <span class="path">/api/ai/upload-model</span>
                <button class="copy-btn" onclick="copyToClipboard('https://' + window.location.host + '/api/ai/upload-model')">Copy URL</button>
                
                <div class="description">
                    <p>Upload a CoreML model trained on your device to be combined with other models on the server. The server will create an ensemble model incorporating multiple uploaded models.</p>
                </div>
                
                <div class="auth-info">
                    <strong>Authentication Required:</strong> Header <code>X-API-Key</code> must be provided.
                </div>
                
                <div class="request-example">
                    <h3>Request Format</h3>
                    <p>This endpoint requires a <code>multipart/form-data</code> request with the following fields:</p>
                    <table>
                        <tr>
                            <th>Field</th>
                            <th>Type</th>
                            <th>Description</th>
                        </tr>
                        <tr>
                            <td>model</td>
                            <td>File</td>
                            <td>The CoreML (.mlmodel) file to upload</td>
                        </tr>
                        <tr>
                            <td>deviceId</td>
                            <td>String</td>
                            <td>The unique identifier of the uploading device</td>
                        </tr>
                        <tr>
                            <td>appVersion</td>
                            <td>String</td>
                            <td>The version of the app sending the model</td>
                        </tr>
                        <tr>
                            <td>description</td>
                            <td>String</td>
                            <td>Optional description of the model</td>
                        </tr>
                    </table>
                </div>
                
                <div class="response-example">
                    <h3>Response Example</h3>
                    <pre>{
  "success": true,
  "message": "Model uploaded successfully. Model will be incorporated in next scheduled training",
  "modelId": "d290f1ee-6c54-4b01-90e6-d701748f0851",
  "latestModelVersion": "1.0.1712052481",
  "modelDownloadURL": "https://yourdomain.com/api/ai/models/1.0.1712052481"
}</pre>
                </div>
                
                <div class="description">
                    <h3>Model Processing</h3>
                    <p>After models are uploaded:</p>
                    <ul>
                        <li>They are stored on the server and queued for processing</li>
                        <li>When enough models are uploaded (3+) or after a time threshold, retraining is triggered</li>
                        <li>The server combines all uploaded models with its base model using ensemble techniques</li>
                        <li>The resulting model is available through the standard model endpoints</li>
                    </ul>
                </div>
            </div>
            
            <!-- GET /api/ai/models/{version} -->
            <div class="endpoint-card">
                <span class="method get">GET</span>
                <span class="path">/api/ai/models/{version}</span>
                <button class="copy-btn" onclick="copyToClipboard('https://' + window.location.host + '/api/ai/models/1.0.0')">Copy URL</button>
                
                <div class="description">
                    <p>Download a specific model version. Returns the CoreML model file.</p>
                </div>
                
                <div class="auth-info">
                    <strong>Authentication Required:</strong> Header <code>X-API-Key</code> must be provided.
                </div>
                
                <div class="parameters">
                    <h3>URL Parameters</h3>
                    <table>
                        <tr>
                            <th>Parameter</th>
                            <th>Description</th>
                        </tr>
                        <tr>
                            <td>version</td>
                            <td>The version of the model to download (e.g., "1.0.0")</td>
                        </tr>
                    </table>
                </div>
                
                <div class="response-example">
                    <h3>Response</h3>
                    <p>Binary file (CoreML model) or error message if model not found.</p>
                </div>
            </div>
            
            <!-- GET /api/ai/latest-model -->
            <div class="endpoint-card">
                <span class="method get">GET</span>
                <span class="path">/api/ai/latest-model</span>
                <button class="copy-btn" onclick="copyToClipboard('https://' + window.location.host + '/api/ai/latest-model')">Copy URL</button>
                
                <div class="description">
                    <p>Get information about the latest trained model. Returns the version and download URL.</p>
                </div>
                
                <div class="auth-info">
                    <strong>Authentication Required:</strong> Header <code>X-API-Key</code> must be provided.
                </div>
                
                <div class="response-example">
                    <h3>Response Example</h3>
                    <pre>{
  "success": true,
  "message": "Latest model info",
  "latestModelVersion": "1.0.1712052481",
  "modelDownloadURL": "https://yourdomain.com/api/ai/models/1.0.1712052481"
}</pre>
                </div>
            </div>
            
            <!-- GET /api/ai/stats -->
            <div class="endpoint-card">
                <span class="method get">GET</span>
                <span class="path">/api/ai/stats</span>
                <button class="copy-btn" onclick="copyToClipboard('https://' + window.location.host + '/api/ai/stats')">Copy URL</button>
                
                <div class="description">
                    <p>Get statistics about the collected data and model training. For admin use only.</p>
                </div>
                
                <div class="auth-info">
                    <strong>Authentication Required:</strong> Header <code>X-Admin-Key</code> must be provided.
                </div>
                
                <div class="response-example">
                    <h3>Response Example</h3>
                    <pre>{
  "success": true,
  "stats": {
    "totalInteractions": 1250,
    "uniqueDevices": 48,
    "averageFeedbackRating": 4.32,
    "topIntents": [
      {"intent": "light_on", "count": 325},
      {"intent": "temperature_query", "count": 214},
      {"intent": "music_play", "count": 186},
      {"intent": "weather_query", "count": 142},
      {"intent": "timer_set", "count": 95}
    ],
    "latestModelVersion": "1.0.1712052481",
    "lastTrainingDate": "2025-04-01T02:00:00Z"
  }
}</pre>
                </div>
            </div>
            
            <!-- GET /health -->
            <div class="endpoint-card">
                <span class="method get">GET</span>
                <span class="path">/health</span>
                <button class="copy-btn" onclick="copyToClipboard('https://' + window.location.host + '/health')">Copy URL</button>
                
                <div class="description">
                    <p>Health check endpoint to verify the server is running properly. Checks database and model storage accessibility.</p>
                </div>
                
                <div class="response-example">
                    <h3>Response Example</h3>
                    <pre>{
  "status": "up",
  "database": "healthy",
  "models": "healthy",
  "timestamp": "2025-04-01T10:15:30Z"
}</pre>
                </div>
            </div>
            
            <footer>
                <p>Backdoor AI Learning Server &copy; 2025</p>
            </footer>
        </div>
        
        <script>
            function copyToClipboard(text) {
                navigator.clipboard.writeText(text).then(function() {
                    var buttons = document.getElementsByClassName('copy-btn');
                    for (var i = 0; i < buttons.length; i++) {
                        buttons[i].textContent = 'Copy URL';
                    }
                    
                    var clickedButton = event.target;
                    var originalText = clickedButton.textContent;
                    clickedButton.textContent = 'Copied!';
                    
                    setTimeout(function() {
                        clickedButton.textContent = originalText;
                    }, 2000);
                }, function(err) {
                    console.error('Could not copy text: ', err);
                });
            }
        </script>
    </body>
    </html>
    '''
    
    return render_template_string(html_template)

if __name__ == '__main__':
    pip_version = subprocess.check_output(["pip", "--version"]).decode("utf-8").strip()
    logger.info(f"Using pip version: {pip_version}")
    logger.info("Starting Backdoor AI Learning Server on Render")
    app.run(host='0.0.0.0', port=PORT, debug=False)