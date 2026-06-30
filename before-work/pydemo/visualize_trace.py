import os
import json
import re
from collections import defaultdict
from html import escape

from task_matcher_utils import (
    get_task_info_by_goal,
    load_template_patterns,
    load_task_metadata,
    get_short_task_label,
    group_traces_experiment,
)


# ================= 辅助函数 =================

def extract_bounds(xml_path, index):
    if not os.path.exists(xml_path):
        return None
    try:
        with open(xml_path, "r", encoding="utf-8") as f:
            content = f.read()
            pattern_json = rf'"unique_id":\s*{index},.*?"bounds_in_screen":\s*\{{"bottom":\s*(\d+),\s*"left":\s*(\d+),\s*"right":\s*(\d+),\s*"top":\s*(\d+)\}}'
            match = re.search(pattern_json, content, re.DOTALL)
            if match:
                bottom, left, right, top = map(int, match.groups())
                return {"left": left, "top": top, "width": right - left, "height": bottom - top}
            bounds_matches = re.findall(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', content)
            if bounds_matches and index < len(bounds_matches):
                left, top, right, bottom = map(int, bounds_matches[index])
                return {"left": left, "top": top, "width": right - left, "height": bottom - top}
    except Exception as e:
        print(f"解析 XML 失败: {e}")
    return None


def parse_action_str(action_str):
    action_data = {"type": "unknown"}
    action_str = str(action_str)
    m_type = re.search(r"action_type='([^']+)'", action_str)
    if m_type:
        action_data["type"] = m_type.group(1)
    m_idx = re.search(r"\bindex=(\d+)", action_str)
    if m_idx:
        action_data["index"] = int(m_idx.group(1))
    m_x = re.search(r"\bx=(\d+(?:\.\d+)?)", action_str)
    m_y = re.search(r"\by=(\d+(?:\.\d+)?)", action_str)
    if m_x and m_y:
        action_data["x"] = float(m_x.group(1))
        action_data["y"] = float(m_y.group(1))
    m_dir = re.search(r"direction='([^']+)'", action_str)
    if m_dir:
        action_data["direction"] = m_dir.group(1)
    m_text = re.search(r"text='([^']+)'", action_str)
    if m_text:
        action_data["text"] = m_text.group(1)
    return action_data


def get_image_src(rel_path):
    return rel_path.replace("\\", "/")


# ================= 主渲染函数 =================

def generate_grouped_report(outputs_dir, templates_map=None, difficulty_map=None, consistency_map=None):
    """
    生成中文界面 HTML 分组轨迹对比报告。
    使用时间戳聚类分组实验组（同一模板类型的不同种子批次被分开）。
    任务名保持原始英文，仅UI元素使用中文。
    """
    if templates_map is None:
        templates_map = load_template_patterns()
    if difficulty_map is None:
        difficulty_map = load_task_metadata()

    # 收集所有轨迹
    all_traces = []
    runs = sorted(
        [d for d in os.listdir(outputs_dir) if os.path.isdir(os.path.join(outputs_dir, d))],
        reverse=True
    )
    for run_dir in runs:
        run_path = os.path.join(outputs_dir, run_dir)
        trace_file = os.path.join(run_path, "trace.json")
        if not os.path.exists(trace_file):
            continue
        with open(trace_file, "r", encoding="utf-8") as f:
            trace_log = json.load(f)
        if not trace_log:
            continue
        goal = trace_log[0].get("goal", "")
        _, task_name = get_task_info_by_goal(goal, templates_map, difficulty_map)
        all_traces.append({
            "id": run_dir,
            "goal": goal,
            "task_name": task_name,
            "steps": trace_log,
        })

    if not all_traces:
        print(f"在 {outputs_dir} 中未找到有效轨迹")
        return

    # 用时间戳聚类得到实验组
    experiment_groups = group_traces_experiment(all_traces, templates_map, difficulty_map)

    total_groups = len(experiment_groups)
    total_runs = len(all_traces)

    # ===== HTML 头部（中文化） =====
    html_parts = []
    html_parts.append("""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Android World - 分组轨迹对比</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'Microsoft YaHei', 'Segoe UI', sans-serif; background: #1e1e1e; color: #e0e0e0; display: flex; height: 100vh; overflow: hidden; }
  .sidebar { width: 330px; min-width: 330px; background: #252526; border-right: 1px solid #333; display: flex; flex-direction: column; }
  .sidebar-header { padding: 16px 20px; background: #333337; text-align: center; font-weight: bold; border-bottom: 1px solid #444; }
  .sidebar-header small { display: block; font-weight: normal; color: #888; font-size: 0.8em; margin-top: 2px; }
  .search-box { padding: 10px; background: #2d2d2d; border-bottom: 1px solid #444; }
  .search-box input { width: 100%; padding: 8px 12px; background: #3c3c3c; color: #e0e0e0; border: 1px solid #555; border-radius: 4px; outline: none; font-size: 14px; box-sizing: border-box; }
  .search-box input:focus { border-color: #007acc; }
  .group-list { flex: 1; overflow-y: auto; }
  .group-item { padding: 12px 16px; border-bottom: 1px solid #333; cursor: pointer; transition: background 0.15s; }
  .group-item:hover { background: #2a2d2e; }
  .group-item.active { background: #37373d; border-left: 4px solid #007acc; }
  .group-item .group-name { font-size: 0.9em; font-weight: bold; color: #e0e0e0; word-break: break-all; }
  .group-item .group-label { font-size: 0.78em; color: #4fc3f7; margin-top: 2px; }
  .group-item .group-meta { font-size: 0.78em; color: #888; margin-top: 2px; }
  .group-item .group-goal { font-size: 0.80em; color: #999; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; margin-top: 3px; }
  .main-content { flex: 1; display: flex; flex-direction: column; overflow: hidden; }
  .group-header { padding: 16px 24px; background: #252526; border-bottom: 1px solid #333; flex-shrink: 0; }
  .group-header .group-title { font-size: 1.2em; font-weight: bold; color: #4fc3f7; }
  .group-header .group-goal { font-size: 0.95em; color: #aaa; font-style: italic; margin-top: 4px; }
  .group-header .group-stats { font-size: 0.85em; color: #888; margin-top: 4px; }
  .runs-scroll { flex: 1; overflow-x: auto; overflow-y: auto; padding: 16px 20px; display: flex; gap: 16px; align-items: flex-start; }
  .run-column { min-width: 280px; max-width: 320px; background: #2d2d2d; border-radius: 8px; border: 1px solid #3a3a3a; overflow: hidden; flex-shrink: 0; }
  .run-column .run-header { padding: 10px 14px; background: #333337; font-size: 0.85em; font-weight: bold; color: #ccc; border-bottom: 1px solid #444; word-break: break-all; }
  .run-column .run-header .rc { color: #888; font-weight: normal; }
  .run-column .run-header .completion { float: right; font-size: 0.8em; padding: 2px 8px; border-radius: 10px; }
  .run-column .run-header .completion.complete { background: #1b5e20; color: #81c784; }
  .run-column .run-header .completion.failed { background: #b71c1c; color: #ef9a9a; }
  .step-row { padding: 8px 12px; border-bottom: 1px solid #3a3a3a; }
  .step-row:last-child { border-bottom: none; }
  .step-row .step-label { font-size: 0.78em; color: #4fc3f7; font-weight: bold; margin-bottom: 4px; }
  .step-row .step-label .step-num { color: #888; }
  .step-row .step-images { display: flex; gap: 6px; justify-content: center; }
  .step-row .step-images .img-wrapper { position: relative; display: inline-block; }
  .step-row .step-images img { max-height: 180px; width: auto; border-radius: 3px; border: 1px solid #444; display: block; }
  .step-row .step-images .img-wrapper .overlays { position: absolute; top: 0; left: 0; width: 100%; height: 100%; pointer-events: none; overflow: hidden; }
  .action-box-overlay { position: absolute; border: 3px solid #ff3333; background-color: rgba(255, 51, 51, 0.2); box-shadow: 0 0 8px rgba(255,0,0,0.7); border-radius: 4px; z-index: 5; box-sizing: border-box; animation: pulseBox 1.5s infinite; }
  .action-dot-overlay { position: absolute; width: 18px; height: 18px; background-color: #ff3333; border: 2px solid white; border-radius: 50%; box-shadow: 0 0 10px rgba(255,0,0,0.8); z-index: 6; transform: translate(-50%, -50%); animation: pulseDot 1.5s infinite; }
  .action-arrow-overlay { position: absolute; z-index: 4; display: flex; justify-content: center; align-items: center; font-weight: bold; color: #fff; font-size: 0.8rem; text-shadow: 1px 1px 3px #000; }
  .action-arrow-overlay span { background: rgba(0,0,0,0.7); padding: 4px 10px; border-radius: 4px; border: 1px solid #007acc; }
  .action-arrow-overlay.up { top: 0; left: 0; width: 100%; height: 35%; background: linear-gradient(to top, transparent, rgba(0,122,204,0.5)); align-items: flex-start; padding-top: 15px; }
  .action-arrow-overlay.down { bottom: 0; left: 0; width: 100%; height: 35%; background: linear-gradient(to bottom, transparent, rgba(0,122,204,0.5)); align-items: flex-end; padding-bottom: 15px; }
  .action-arrow-overlay.left { top: 0; left: 0; width: 35%; height: 100%; background: linear-gradient(to left, transparent, rgba(0,122,204,0.5)); justify-content: flex-start; padding-left: 15px; }
  .action-arrow-overlay.right { top: 0; right: 0; width: 35%; height: 100%; background: linear-gradient(to right, transparent, rgba(0,122,204,0.5)); justify-content: flex-end; padding-right: 15px; }
  .action-text-overlay { position: absolute; background: #ff9800; color: #1e1e1e; padding: 4px 8px; border-radius: 4px; font-size: 12px; font-weight: bold; z-index: 10; white-space: nowrap; box-shadow: 0 3px 8px rgba(0,0,0,0.6); border: 1px solid white; }
  @keyframes pulseBox { 0% { box-shadow: 0 0 0 0 rgba(255,51,51,0.7); } 70% { box-shadow: 0 0 0 8px rgba(255,51,51,0); } 100% { box-shadow: 0 0 0 0 rgba(255,51,51,0); } }
  @keyframes pulseDot { 0% { transform: translate(-50%,-50%) scale(1); box-shadow: 0 0 0 0 rgba(255,51,51,0.7); } 50% { transform: translate(-50%,-50%) scale(1.3); box-shadow: 0 0 0 10px rgba(255,51,51,0); } 100% { transform: translate(-50%,-50%) scale(1); box-shadow: 0 0 0 0 rgba(255,51,51,0); } }
  .step-row .step-action { font-size: 0.75em; color: #999; font-family: Consolas, monospace; word-break: break-all; margin-top: 4px; max-height: 2.4em; overflow: hidden; }
  .badge-click { display: inline-block; padding: 1px 6px; border-radius: 3px; font-size: 0.85em; font-weight: bold; background: #1565c0; color: #90caf9; }
  .badge-input_text { display: inline-block; padding: 1px 6px; border-radius: 3px; font-size: 0.85em; font-weight: bold; background: #e65100; color: #ffcc80; }
  .badge-scroll { display: inline-block; padding: 1px 6px; border-radius: 3px; font-size: 0.85em; font-weight: bold; background: #2e7d32; color: #a5d6a7; }
  .badge-open_app { display: inline-block; padding: 1px 6px; border-radius: 3px; font-size: 0.85em; font-weight: bold; background: #6a1b9a; color: #ce93d8; }
  .badge-long_press { display: inline-block; padding: 1px 6px; border-radius: 3px; font-size: 0.85em; font-weight: bold; background: #c62828; color: #ef9a9a; }
  .badge-status { display: inline-block; padding: 1px 6px; border-radius: 3px; font-size: 0.85em; font-weight: bold; background: #37474f; color: #90a4ae; }
  .badge-answer { display: inline-block; padding: 1px 6px; border-radius: 3px; font-size: 0.85em; font-weight: bold; background: #004d40; color: #80cbc4; }
  .badge-unknown { display: inline-block; padding: 1px 6px; border-radius: 3px; font-size: 0.85em; font-weight: bold; background: #555; color: #ccc; }
  .badge-swipe { display: inline-block; padding: 1px 6px; border-radius: 3px; font-size: 0.85em; font-weight: bold; background: #0277bd; color: #81d4fa; }
  .badge-consistency-0 { display: inline-block; padding: 1px 6px; border-radius: 3px; font-size: 0.75em; font-weight: bold; background: #1b5e20; color: #81c784; }
  .badge-consistency-1 { display: inline-block; padding: 1px 6px; border-radius: 3px; font-size: 0.75em; font-weight: bold; background: #e65100; color: #ffcc80; }
  .badge-consistency-2 { display: inline-block; padding: 1px 6px; border-radius: 3px; font-size: 0.75em; font-weight: bold; background: #9b59b6; color: #d2b4de; }
  .badge-consistency-3 { display: inline-block; padding: 1px 6px; border-radius: 3px; font-size: 0.75em; font-weight: bold; background: #b71c1c; color: #ef9a9a; }
  .group-display { display: none; height: 100%; flex-direction: column; }
  .group-display.active { display: flex; }
</style>
</head>
<body>

<div class="sidebar">
  <div class="sidebar-header">
    任务分组
    <small>""")
    html_parts.append(f"{total_groups} 组, {total_runs} 条轨迹")
    html_parts.append("""</small>
  </div>
  <div class="search-box">
    <input type="text" id="searchInput" placeholder="筛选...">
  </div>
  <div class="group-list" id="groupList">
""")

    # ===== 侧边栏：实验组（原始英文任务名） =====
    for gi, group in enumerate(experiment_groups):
        task_name = group["task_name"]
        sample_goal = escape(group["goal"])
        active_attr = "active" if gi == 0 else ""
        safe_name = task_name.replace("'", "\\'").replace('"', "&quot;")
        safe_id = f"{task_name}_{gi}".replace("'", "\\'").replace('"', "&quot;")
        group_consistency = None
        if consistency_map:
            for trace_obj in group["traces"]:
                tid = trace_obj.get("id", "")
                if tid in consistency_map:
                    group_consistency = consistency_map[tid]
                    break
        consistency_html = ""
        if group_consistency:
            ct = group_consistency["type"]
            cl = escape(group_consistency["label"])
            consistency_html = f'<div class="group-meta" style="margin-top:4px;"><span class="badge-consistency-{ct}">一致性: {cl}</span></div>'
        html_parts.append(f"""    <div class="group-item {active_attr}" onclick="switchGroup('{safe_id}')" id="menu-{safe_id}">
      <div class="group-name">{escape(task_name)}</div>
      <div class="group-meta">实验组 #{gi+1} | {group['count']} 条轨迹</div>
      {consistency_html}
      <div class="group-goal">{sample_goal[:100]}{'...' if len(sample_goal) > 100 else ''}</div>
    </div>
""")

    html_parts.append("""  </div>
</div>

<div class="main-content" id="mainContent">
""")

    # ===== 主内容区 =====
    first_group_id = None
    for gi, group in enumerate(experiment_groups):
        group_id = f"grp-{gi}"
        if gi == 0:
            first_group_id = group_id
        task_name = group["task_name"]
        sample_goal = escape(group["goal"])
        active_cls = "active" if gi == 0 else ""
        short_label = escape(get_short_task_label(task_name))

        gc = None
        if consistency_map:
            for trace_obj in group["traces"]:
                tid = trace_obj.get("id", "")
                if tid in consistency_map:
                    gc = consistency_map[tid]
                    break
        consistency_main_html = ""
        if gc:
            ct = gc["type"]
            cl = escape(gc["label"])
            consistency_main_html = f'<div class="group-meta" style="margin-top:4px;"><span class="badge-consistency-{ct}">一致性: {cl}</span></div>'
        html_parts.append(f"""  <div class="group-display {active_cls}" id="{group_id}">
    <div class="group-header">
      <div class="group-title">{escape(task_name)}</div>
      <div class="group-goal">目标: {sample_goal}</div>
      <div class="group-stats">实验组 #{gi+1} | {escape(short_label)} | {group['count']} 条轨迹</div>
      {consistency_main_html}
    </div>
    <div class="runs-scroll">
""")

        traces = group["traces"]
        for trace in traces:
            steps = trace["steps"]
            n_steps = len(steps)
            last_action = str(steps[-1].get("action", ""))
            is_completed = "action_type='status'" in last_action and "goal_status='complete'" in last_action
            if is_completed:
                completion_html = '<span class="completion complete">已完成</span>'
            else:
                completion_html = '<span class="completion failed">未完成</span>'
            short_id = trace["id"][-8:] if len(trace["id"]) > 8 else trace["id"]

            html_parts.append(f"""      <div class="run-column">
        <div class="run-header">
          <span class="rc">#{escape(short_id)}</span> {completion_html}
        </div>
""")

            for si, step in enumerate(steps):
                img_before_rel = os.path.join(trace["id"], "images", step["image_before"]).replace("\\", "/")
                img_after_rel = os.path.join(trace["id"], "images", step["image_after"]).replace("\\", "/")
                img_before_b64 = get_image_src(img_before_rel)
                img_after_b64 = get_image_src(img_after_rel)

                action_str = str(step.get("action", ""))
                action_data = parse_action_str(action_str)

                if "index" in action_data:
                    xml_path = os.path.join(outputs_dir, trace["id"], "xmls", f"{si}_before.xml")
                    action_bounds = extract_bounds(xml_path, action_data["index"])
                    if action_bounds:
                        action_data["bounds"] = action_bounds

                action_json_str = json.dumps(action_data).replace("'", "&#39;")
                action_type = escape(action_data.get("type", "unknown"))
                action_text = escape(action_str)
                action_type_cn = {
                    "click": "点击", "input_text": "输入", "scroll": "滚动",
                    "open_app": "打开应用", "long_press": "长按", "status": "状态",
                    "answer": "回答", "swipe": "滑动", "unknown": "未知",
                }.get(action_type, action_type)
                badge_class = f"badge-{action_type}" if action_type in (
                    "click", "input_text", "scroll", "open_app", "long_press",
                    "status", "answer", "swipe"
                ) else "badge-unknown"

                html_parts.append(f"""        <div class="step-row" data-action='{action_json_str}'>
          <div class="step-label"><span class="step-num">第{si+1}/{n_steps}步</span>  <span class="{badge_class}">{action_type_cn}({action_type})</span></div>
          <div class="step-images">
            <div class="img-wrapper">
              <img src="{img_before_b64}" class="before-img" loading="lazy" onload="updateOverlay(this)">
              <div class="overlays"></div>
            </div>
            <div class="img-wrapper">
              <img src="{img_after_b64}" loading="lazy">
            </div>
          </div>
          <div class="step-action">{action_text}</div>
        </div>
""")

            html_parts.append("""      </div>
""")

        html_parts.append("""    </div>
  </div>
""")

    # ===== JavaScript =====
    js_first_group = json.dumps(first_group_id if first_group_id else "grp-0")
    html_parts.append(f"""</div>

<script>
let currentGroupId = {js_first_group};

function switchGroup(groupName) {{
  const items = document.querySelectorAll('.group-item');
  let targetIdx = -1;
  items.forEach((item, idx) => {{
    if (item.id === 'menu-' + groupName) {{
      targetIdx = idx;
    }}
  }});
  if (targetIdx < 0) return;

  const curDisplay = document.getElementById(currentGroupId);
  if (curDisplay) curDisplay.classList.remove('active');
  const curMenu = document.querySelector('.group-item.active');
  if (curMenu) curMenu.classList.remove('active');

  const newDisplay = document.getElementById('grp-' + targetIdx);
  if (newDisplay) newDisplay.classList.add('active');
  const newMenu = document.getElementById('menu-' + groupName);
  if (newMenu) newMenu.classList.add('active');

  currentGroupId = 'grp-' + targetIdx;

  const display = document.getElementById(currentGroupId);
  if (display) {{
    display.querySelectorAll('.before-img').forEach(img => {{
      if (img.complete) {{
        updateOverlay(img);
      }}
    }});
  }}
}}

function updateOverlay(img) {{
  const stepRow = img.closest('[data-action]');
  const overlays = img.parentElement.querySelector('.overlays');
  if (!stepRow || !overlays) return;

  overlays.innerHTML = '';

  const actionStr = stepRow.getAttribute('data-action');
  if (!actionStr || actionStr === 'null' || actionStr === '') return;

  let action;
  try {{ action = JSON.parse(actionStr); }} catch(e) {{ return; }}

  const naturalW = img.naturalWidth || 1080;
  const naturalH = img.naturalHeight || 1920;
  const clientW = img.clientWidth;
  const clientH = img.clientHeight;

  if (clientW === 0 || clientH === 0) return;

  const scale = Math.min(clientW / naturalW, clientH / naturalH);
  const renderedW = naturalW * scale;
  const renderedH = naturalH * scale;
  const offsetX = (clientW - renderedW) / 2;
  const offsetY = (clientH - renderedH) / 2;

  if (action.bounds) {{
    let box = document.createElement('div');
    box.className = 'action-box-overlay';
    box.style.left = (offsetX + action.bounds.left * scale) + 'px';
    box.style.top = (offsetY + action.bounds.top * scale) + 'px';
    box.style.width = (action.bounds.width * scale) + 'px';
    box.style.height = (action.bounds.height * scale) + 'px';
    overlays.appendChild(box);
  }}

  if (!action.bounds && action.x !== undefined && action.y !== undefined) {{
    let dot = document.createElement('div');
    dot.className = 'action-dot-overlay';
    dot.style.left = (offsetX + action.x * scale) + 'px';
    dot.style.top = (offsetY + action.y * scale) + 'px';
    overlays.appendChild(dot);
  }}

  if (action.type === 'scroll' || action.type === 'swipe') {{
    if (action.direction) {{
      let arrow = document.createElement('div');
      arrow.className = 'action-arrow-overlay ' + action.direction;
      const dirCN = {{'up': '上', 'down': '下', 'left': '左', 'right': '右'}};
      const dirLabel = dirCN[action.direction] || action.direction;
      arrow.innerHTML = '<span>' + action.type.toUpperCase() + ': ' + dirLabel + '</span>';
      overlays.appendChild(arrow);
    }}
  }}

  if (action.type === 'input_text' && action.text) {{
    let textLabel = document.createElement('div');
    textLabel.className = 'action-text-overlay';
    textLabel.innerText = '输入: ' + action.text;
    if (action.bounds) {{
      textLabel.style.left = (offsetX + action.bounds.left * scale) + 'px';
      textLabel.style.top = Math.max(0, offsetY + action.bounds.top * scale - 30) + 'px';
    }} else if (action.x !== undefined && action.y !== undefined) {{
      textLabel.style.left = (offsetX + action.x * scale) + 'px';
      textLabel.style.top = Math.max(0, offsetY + action.y * scale - 30) + 'px';
    }} else {{
      textLabel.style.left = '50%';
      textLabel.style.top = '10%';
      textLabel.style.transform = 'translateX(-50%)';
    }}
    overlays.appendChild(textLabel);
  }}
}}

let resizeTimer;
window.addEventListener('resize', () => {{
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => {{
    const display = document.getElementById(currentGroupId);
    if (display) {{
      display.querySelectorAll('.before-img').forEach(img => {{
        if (img.complete) updateOverlay(img);
      }});
    }}
  }}, 200);
}});

document.getElementById('searchInput').addEventListener('input', function() {{
  const filter = this.value.toLowerCase();
  const items = document.querySelectorAll('.group-item');
  let firstVisible = null;
  items.forEach(function(item) {{
    const name = item.querySelector('.group-name').textContent.toLowerCase();
    const goal = item.querySelector('.group-goal').textContent.toLowerCase();
    const match = name.includes(filter) || goal.includes(filter);
    item.style.display = match ? '' : 'none';
    if (match && !firstVisible) firstVisible = item;
  }});
  const activeItem = document.querySelector('.group-item.active');
  if (activeItem && activeItem.style.display === 'none' && firstVisible) {{
    const groupName = firstVisible.id.replace('menu-', '');
    switchGroup(groupName);
  }}
}});
</script>

</body>
</html>
""")

    html_content = "".join(html_parts)
    report_path = os.path.join(outputs_dir, "grouped_report.html")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(html_content)
    print(f"分组轨迹报告已生成: {report_path}")


if __name__ == "__main__":
    script_dir = os.path.dirname(os.path.abspath(__file__))
    # Load consistency data if available
    consistency_data = None
    cjson_path = os.path.join(script_dir, "trajectory_consistency.json")
    if os.path.exists(cjson_path):
        try:
            with open(cjson_path, "r", encoding="utf-8") as f:
                consistency_data = json.load(f)
        except:
            pass

    found = False
    for name in os.listdir(script_dir):
        path = os.path.join(script_dir, name)
        if os.path.isdir(path) and name.startswith("outputs"):
            print(f"处理中: {path}")
            dirname = os.path.basename(path)
            consistency_map = consistency_data.get(dirname, {}) if consistency_data else {}
            generate_grouped_report(path, consistency_map=consistency_map)
            found = True
    if not found:
        print("未找到 outputs* 目录")