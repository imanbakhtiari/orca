from flask import Flask, render_template, request, redirect, url_for
from flask_apscheduler import APScheduler
from prometheus_client import Gauge, generate_latest
from flask_sqlalchemy import SQLAlchemy
import subprocess
import requests
import time

app = Flask(__name__)

# Configure SQLite database
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///monitoring.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# APScheduler configuration
scheduler = APScheduler()
scheduler.api_enabled = True
scheduler.init_app(app)
scheduler.start()

# Prometheus metrics
http_response_time_gauge = Gauge(
    "http_response_time_ms",
    "Response time for HTTP requests in milliseconds",
    ["target"]
)
http_status_code_gauge = Gauge(
    "http_status_code",
    "HTTP status code for monitored targets",
    ["target"]
)
http_success_gauge = Gauge(
    "http_success",
    "HTTP success (1 for match, 0 for failure)",
    ["target"]
)
http_content_length_gauge = Gauge(
    "http_content_length",
    "Content length of HTTP response",
    ["target"]
)
icmp_latency_gauge = Gauge(
    "icmp_latency_ms",
    "ICMP ping latency in milliseconds",
    ["target"]
)
icmp_success_gauge = Gauge(
    "icmp_success",
    "ICMP success (1 for success, 0 for failure)",
    ["target"]
)
icmp_packet_loss_gauge = Gauge(
    "icmp_packet_loss",
    "Packet loss percentage for ICMP",
    ["target"]
)
nslookup_time_gauge = Gauge(
    "dns_lookup_time_ms",
    "DNS resolution time in milliseconds",
    ["target"]
)

# Database model
class MonitoringTask(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    target = db.Column(db.String(255), nullable=False)
    method = db.Column(db.String(50), nullable=False)  # ICMP, HTTP, or DNS
    interval = db.Column(db.Integer, nullable=False)
    timeout = db.Column(db.Integer, nullable=False)
    expected_status = db.Column(db.Integer, nullable=True)  # Only relevant for HTTP

# In-memory cache for monitoring tasks
monitoring_tasks = {}

def load_tasks_from_db():
    """Load all tasks from the database into the in-memory dictionary."""
    tasks = MonitoringTask.query.all()
    for task in tasks:
        task_id = f"task_{task.id}"
        monitoring_tasks[task_id] = {
            "id": task.id,
            "target": task.target,
            "type": task.method,
            "interval": task.interval,
            "timeout": task.timeout,
            "expected_status": task.expected_status,
            "last_result": {
                "target": task.target,
                "type": task.method,
                # We'll fill these keys as checks run:
                "icmp_latency_ms": None,
                "status_match": None,
                "response_time_ms": None,
                "dns_lookup_ms": None
            }
        }

def nslookup_target(task_id, target, timeout):
    """
    Perform an nslookup to measure DNS resolution time for the target.
    Updates Prometheus gauge with the measured duration in ms.
    """
    print(f"[NSLOOKUP] Resolving {target} (Task ID: {task_id})...")
    start_time = time.time()

    try:
        result = subprocess.run(
            ["nslookup", target],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout
        )
        end_time = time.time()
        duration_ms = (end_time - start_time) * 1000.0

        print(f"[NSLOOKUP RAW OUTPUT]\n{result.stdout.strip()}")
        nslookup_time_gauge.labels(target=target).set(duration_ms)
        print(f"[NSLOOKUP] {target} resolved in {duration_ms:.2f} ms")

        monitoring_tasks[task_id]['last_result']['dns_lookup_ms'] = duration_ms

    except subprocess.TimeoutExpired:
        print(f"[NSLOOKUP] {target} resolution timed out")
        nslookup_time_gauge.labels(target=target).set(0)
        monitoring_tasks[task_id]['last_result']['dns_lookup_ms'] = None

    except Exception as e:
        print(f"[NSLOOKUP] Error resolving {target}: {e}")
        nslookup_time_gauge.labels(target=target).set(0)
        monitoring_tasks[task_id]['last_result']['dns_lookup_ms'] = None

def ping_target(task_id, target, timeout):
    """Perform an ICMP ping."""
    try:
        print(f"[PING] Pinging {target} (Task ID: {task_id})...")
        ping_process = subprocess.run(
            ["ping", "-c", "1", "-W", str(timeout), target],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        print(f"[PING RAW OUTPUT] {ping_process.stdout}")

        icmp_latency = None
        success = 0
        packet_loss = 100

        for line in ping_process.stdout.splitlines():
            if "time=" in line:
                icmp_latency = line.split("time=")[-1].split(" ")[0]
                success = 1
            if "packet loss" in line:
                packet_loss = float(line.split('%')[0].split()[-1])

        print(f"[PING] Result for {target}: {icmp_latency} ms, Success: {success}")
        monitoring_tasks[task_id]['last_result'] = {
            "target": target,
            "type": "ICMP",
            "icmp_latency_ms": icmp_latency,
            "ping_output": ping_process.stdout.strip()
        }
        icmp_latency_gauge.labels(target=target).set(float(icmp_latency) if icmp_latency else 0)
        icmp_success_gauge.labels(target=target).set(success)
        icmp_packet_loss_gauge.labels(target=target).set(packet_loss)

    except Exception as e:
        print(f"[PING] Error pinging {target}: {e}")
        monitoring_tasks[task_id]['last_result'] = {"error": str(e)}
        icmp_success_gauge.labels(target=target).set(0)

def curl_target(task_id, target, timeout, expected_status):
    """Perform an HTTP GET request using requests."""
    try:
        print(f"[HTTP] Sending request to {target} (Task ID: {task_id})...")
        if not target.startswith("http://") and not target.startswith("https://"):
            target = "http://" + target

        http_start_time = time.time()
        response = requests.get(target, timeout=timeout)
        http_end_time = time.time()
        response_time = (http_end_time - http_start_time) * 1000.0
        status_match = (response.status_code == expected_status)

        print(f"[HTTP] Result for {target}: {response.status_code} in {response_time:.2f} ms, Match: {status_match}")
        monitoring_tasks[task_id]['last_result'] = {
            "target": target,
            "type": "HTTP",
            "response_time_ms": response_time,
            "status_code": response.status_code,
            "status_match": status_match,
            "content_length": len(response.content),
            "headers": dict(response.headers)
        }
        http_response_time_gauge.labels(target=target).set(response_time)
        http_status_code_gauge.labels(target=target).set(response.status_code)
        http_success_gauge.labels(target=target).set(1 if status_match else 0)
        http_content_length_gauge.labels(target=target).set(len(response.content))

    except Exception as e:
        print(f"[HTTP] Error requesting {target}: {e}")
        monitoring_tasks[task_id]['last_result'] = {"error": str(e)}
        http_success_gauge.labels(target=target).set(0)

def monitor_tasks():
    """Scheduler entry point: loop over all tasks, run the relevant checks."""
    print("[SCHEDULER] Running monitoring tasks...")
    for task_id, task in monitoring_tasks.items():
        # Always do DNS resolution if you want for *all* tasks:
        # nslookup_target(task_id, task['target'], task['timeout'])
        #
        # Or do it conditionally:
        if task['type'] == "DNS":
            nslookup_target(task_id, task['target'], task['timeout'])
        elif task['type'] == "ICMP":
            ping_target(task_id, task['target'], task['timeout'])
        elif task['type'] == "HTTP":
            curl_target(task_id, task['target'], task['timeout'], task['expected_status'])

# Schedule monitoring tasks every 10 seconds
scheduler.add_job(id='monitor_task', func=monitor_tasks, trigger='interval', seconds=10)

@app.route('/')
def index():
    """Main page: list tasks and show form to add new tasks."""
    return render_template('index.html', tasks=monitoring_tasks)

@app.route('/add', methods=['POST'])
def add_task():
    """Add a new monitoring task (ICMP, HTTP, or DNS) to the DB and in-memory dict."""
    target = request.form.get('target')
    method = request.form.get('method')  # ICMP, HTTP, or DNS
    interval = int(request.form.get('interval', 60))
    timeout = int(request.form.get('timeout', 5))
    expected_status = int(request.form.get('expected_status', 200))

    if target and method:
        if method != "HTTP":
            # For DNS/ICMP, expected_status doesn't apply
            expected_status_db = None
        else:
            expected_status_db = expected_status

        new_task = MonitoringTask(
            target=target,
            method=method,
            interval=interval,
            timeout=timeout,
            expected_status=expected_status_db
        )
        db.session.add(new_task)
        db.session.commit()

        # Add to in-memory dictionary
        task_id = f"task_{new_task.id}"
        monitoring_tasks[task_id] = {
            "id": new_task.id,
            "target": target,
            "type": method,
            "interval": interval,
            "timeout": timeout,
            "expected_status": expected_status_db,
            "last_result": {
                "target": target,
                "type": method,
                "icmp_latency_ms": None,
                "status_match": None,
                "response_time_ms": None,
                "dns_lookup_ms": None
            }
        }

    return redirect(url_for('index'))

@app.route('/monitor')
def monitor():
    """Monitoring results page: shows last results in a table that auto-refreshes."""
    return render_template('monitor.html', tasks=monitoring_tasks)

@app.route('/monitor/data')
def monitor_data():
    """Endpoint that returns the in-memory tasks as JSON (for the AJAX refresh)."""
    return {task_id: task for task_id, task in monitoring_tasks.items()}

@app.route('/metrics')
def metrics():
    """Prometheus metrics endpoint."""
    return generate_latest(), 200, {'Content-Type': 'text/plain; charset=utf-8'}

@app.route('/remove/<task_id>')
def remove_task(task_id):
    """
    Remove a task from the DB and from the in-memory dictionary.
    This ensures the scheduler won't check it anymore.
    """
    task_id_num = int(task_id.split("_")[-1])  # e.g., 'task_11' -> 11
    db_task = MonitoringTask.query.get(task_id_num)
    if db_task:
        db.session.delete(db_task)
        db.session.commit()
    # Also remove from memory
    monitoring_tasks.pop(task_id, None)
    return redirect(url_for('index'))

if __name__ == '__main__':
    # Initialize DB and load tasks on startup
    with app.app_context():
        db.create_all()
        load_tasks_from_db()
    # Start the Flask app
    app.run(debug=True)

