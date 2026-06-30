import subprocess
import time
import os
import threading
import sys
import shutil
from multiprocessing import Process

# ================= 配置 =================
TASKS_FILE = "all_tasks.txt"
MAX_PARALLEL = 4  # 四路模拟器并行
MAX_RETRIES = 4  # 单次尝试失败后的重试

BASE_OUTPUT_DIR = "outputs"  # 运行时统一存放轨迹的目录
LOGS_DIR = "logs"  # 运行时统一存放日志的目录
BASE_TMP_DIR = "/data/user_home/predata/xgx/android_world/tmp"

# vLLM 服务配置 
VLLM_ENDPOINT = "http://localhost:28000/v1"
VLLM_API_KEY = "123456"
MODEL_NAME = "qwen3-vl-8b"

# 4 个端口
RESOURCE_POOL = [
    (5554, 8554),
    (5556, 8555),
    (5558, 8556),
    (5560, 8557)
]

# 定义模式顺序：跑完一种模式的所有任务，整理归档，再跑下一种
MODES = [
    {"temp": 0.0, "random_seed": False, "desc": "T0.0_FixedSeed"},
    {"temp": 0.0, "random_seed": True, "desc": "T0.0_RandomSeed"},
    {"temp": 1.0, "random_seed": False, "desc": "T1.0_FixedSeed"},
    {"temp": 1.0, "random_seed": True, "desc": "T1.0_RandomSeed"},

]

def _start_periodic_tmp_cleanup(device_id, interval_sec=300):
  """Start a daemon thread that periodically cleans /tmp/android-* orphan dirs."""
  import glob as _glob
  import shutil as _shutil
  stop_event = threading.Event()
  def _clean_loop():
    while not stop_event.wait(interval_sec):
      for d in _glob.glob('/tmp/android-*'):
        try:
          _shutil.rmtree(d, ignore_errors=True)
          print(f"[{device_id}] Periodic cleanup removed: {d}")
        except Exception:
          pass
  t = threading.Thread(target=_clean_loop, daemon=True)
  t.start()
  return stop_event




def wait_for_device(adb_port):
    device_id = f"emulator-{adb_port}"
    print(f"[{device_id}] 等待设备上线...")
    subprocess.run(["adb", "-s", device_id, "wait-for-device"], timeout=60)
    for _ in range(20):
        try:
            res = subprocess.run(
                ["adb", "-s", device_id, "shell", "getprop", "sys.boot_completed"],
                capture_output=True, text=True, timeout=5
            )
            if "1" in res.stdout:
                time.sleep(10)  # 额外缓冲
                return True
        except:
            pass
        time.sleep(2)
    return False

def setup_device_state(adb_port):
    device_id = f"emulator-{adb_port}"
    subprocess.run(["adb", "-s", device_id, "shell", "input", "keyevent", "KEYCODE_WAKEUP"])
    subprocess.run(["adb", "-s", device_id, "shell", "wm", "dismiss-keyguard"])
    subprocess.run(
        ["adb", "-s", device_id, "shell", "settings", "put", "secure", "enabled_accessibility_services", "null"])
    time.sleep(2)
    a11y = "com.google.androidenv.accessibilityforwarder/.AccessibilityForwarder"
    subprocess.run(
        ["adb", "-s", device_id, "shell", "settings", "put", "secure", "enabled_accessibility_services", a11y])
    subprocess.run(["adb", "-s", device_id, "shell", "settings", "put", "secure", "accessibility_enabled", "1"])
    time.sleep(2)

def prune_empty_dirs(path):

    if not os.path.isdir(path):
        return
    for item in os.listdir(path):
        item_path = os.path.join(path, item)
        if os.path.isdir(item_path):
            prune_empty_dirs(item_path)
    if not os.listdir(path):
        try:
            os.rmdir(path)
        except:
            pass

def run_single_instance(task_name, worker_id, mode_config, adb_port, grpc_port):
    llm_temp = mode_config["temp"]
    is_random_seed = mode_config["random_seed"]

    device_id = f"emulator-{adb_port}"
    timestamp = int(time.time())

    abs_output_dir = os.path.abspath(BASE_OUTPUT_DIR)
    abs_logs_dir = os.path.abspath(LOGS_DIR)

    run_label = f"{task_name}_w{worker_id}_{timestamp}"
    task_output_dir = os.path.join(abs_output_dir, run_label)
    os.makedirs(task_output_dir, exist_ok=True)

    execution_log = os.path.join(abs_logs_dir, f"{run_label}.log")
    unique_seed = timestamp + worker_id if is_random_seed else 0

    worker_tmp = os.path.join(BASE_TMP_DIR, f"worker_{adb_port}")
    os.makedirs(worker_tmp, exist_ok=True)

    emu_env = os.environ.copy()
    for k in ['http_proxy', 'https_proxy', 'all_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY']:
        emu_env.pop(k, None)
    emu_env['TMPDIR'] = worker_tmp
    emu_env['ANDROID_TMP'] = worker_tmp
    emu_env['ANDROID_EMULATOR_TMPDIR'] = worker_tmp
    emu_env['ANDROID_WORLD_TMPDIR'] = worker_tmp

    emu_cmd = [
        "emulator", "-avd", "AndroidWorldAvd", "-snapshot", "clean_setup",
        "-read-only", "-no-snapshot-save", "-no-window", "-gpu", "off",
        "-no-audio", "-no-boot-anim", "-port", str(adb_port), "-grpc", str(grpc_port),
    ]

    emu_proc = subprocess.Popen(emu_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=emu_env)

    _stop_cleanup = _start_periodic_tmp_cleanup(run_label)

    success = False
    try:
        if wait_for_device(adb_port):
            setup_device_state(adb_port)

            run_env = os.environ.copy()
            run_env['TMPDIR'] = worker_tmp
            run_env['ANDROID_WORLD_TMPDIR'] = worker_tmp
            run_env['no_proxy'] = "localhost,127.0.0.1,::1"
            run_env['NO_PROXY'] = "localhost,127.0.0.1,::1"
            run_env['LLM_TEMPERATURE'] = str(llm_temp)
            
         
            run_env['OPENAI_API_BASE'] = VLLM_ENDPOINT
            run_env['OPENAI_API_KEY'] = VLLM_API_KEY  
            run_env['VLLM_MODEL_NAME'] = MODEL_NAME   

            run_cmd = [
                "python", "run.py",
                f"--tasks={task_name}",
                "--suite_family=android_world",
                "--agent_name=m3a_local",
                f"--task_random_seed={unique_seed}",
                f"--grpc_port={grpc_port}",
                f"--console_port={adb_port}",
                f"--output_path={task_output_dir}"
            ]

            print(f"[{run_label}] 运行 run.py... (vLLM, Temp={llm_temp})")
            with open(execution_log, "w") as f:
                res = subprocess.run(run_cmd, stdout=f, stderr=subprocess.STDOUT, env=run_env, cwd=os.getcwd())
                success = (res.returncode == 0)
    finally:
        _stop_cleanup.set()
        subprocess.run(["adb", "-s", device_id, "emu", "kill"], capture_output=True)
        time.sleep(2)
        if emu_proc.poll() is None: 
            emu_proc.kill()
            emu_proc.wait(timeout=5)
            
        # Fallback: clean /tmp/android-* QEMU emulator temp files
        import glob
        import shutil as _shutil
        for pattern in ['/tmp/android-*']:
            for d in glob.glob(pattern):
                try:
                    _shutil.rmtree(d, ignore_errors=True)
                    print(f"[{run_label}] Cleaned orphan emulator tmp: {d}")
                except Exception:
                    pass
        
        
        try: 
            if os.path.exists(worker_tmp): 
                import shutil 
                shutil.rmtree(worker_tmp) 
                print(f"[{run_label}]  已彻底清空残余缓存: {worker_tmp}") 
        except Exception as e: 
            print(f"[{run_label}]  清理缓存失败: {e}")

    summary_path = os.path.join(abs_logs_dir, "task_summary.log")
    with open(summary_path, "a") as f:
        status = "SUCCESS" if success else "FAILED"
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} | {task_name} | Worker {worker_id} | Seed {unique_seed} | {status}\n")

if __name__ == "__main__":
    if not os.path.exists(TASKS_FILE):
        print(f"错误: 找不到 {TASKS_FILE}")
        sys.exit(1)

    with open(TASKS_FILE, "r") as f:
        all_tasks = [line.strip() for line in f if line.strip()]

    print(f"总任务数: {len(all_tasks)} | 模式数: {len(MODES)} | 每次任务并行数: {MAX_PARALLEL}")
    os.makedirs(BASE_TMP_DIR, exist_ok=True)

    for mode in MODES:
        mode_desc = mode['desc']
        print(f"\n >>> 开始运行模式: {mode_desc} (vLLM Local) <<<")

        if os.path.exists(BASE_OUTPUT_DIR): shutil.rmtree(BASE_OUTPUT_DIR)
        if os.path.exists(LOGS_DIR): shutil.rmtree(LOGS_DIR)
        os.makedirs(BASE_OUTPUT_DIR, exist_ok=True)
        os.makedirs(LOGS_DIR, exist_ok=True)

        for task in all_tasks:
            print(f"\n   正在执行任务: {task} (4路并行中...)")
            processes = []
            for i in range(MAX_PARALLEL):
                adb_p, grpc_p = RESOURCE_POOL[i]
                p = Process(target=run_single_instance, args=(task, i, mode, adb_p, grpc_p))
                p.start()
                processes.append(p)
                time.sleep(60)  # 增加到 60s，给 vLLM 充足的时间回收显存和处理 KV Cache
            for p in processes:
                p.join()

        print(f"\n模式 {mode_desc} 全部任务已跑完！开始整理并归档目录...")
        time.sleep(5)

        try:
            prune_empty_dirs(BASE_OUTPUT_DIR)
            target_logs = os.path.join(os.path.abspath(BASE_OUTPUT_DIR), "logs")
            shutil.move(os.path.abspath(LOGS_DIR), target_logs)

            final_dir_name = f"outputs-{mode_desc}"
            if os.path.exists(final_dir_name):
                shutil.rmtree(final_dir_name)
            os.rename(BASE_OUTPUT_DIR, final_dir_name)
            print(f"  └─ 归档完成！当前模式所有数据已保存在: {final_dir_name}/\n")

        except Exception as e:
            print(f"  目录整理发生异常: {e}")
