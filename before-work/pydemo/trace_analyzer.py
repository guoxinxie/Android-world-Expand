import os
import re
import json
import argparse
import platform
import subprocess
import sys
from collections import defaultdict, Counter
from datetime import datetime

import numpy as np
import pandas as pd
import matplotlib
import matplotlib.pyplot as plt
import seaborn as sns
from matplotlib.ticker import MaxNLocator
from html import escape

if platform.system() == "Windows":
    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "sans-serif"]
else:
    plt.rcParams["font.sans-serif"] = ["DejaVu Sans", "SimHei", "sans-serif"]
plt.rcParams["axes.unicode_minus"] = False

CONSISTENCY_LABELS = {0: "完全一致", 1: "高度一致", 2: "中等相似", 3: "严重分歧"}


# ================= XML / 控件解析 =================

def parse_xml_nodes(xml_path):
    if not os.path.exists(xml_path):
        return []
    try:
        with open(xml_path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception:
        return []
    nodes = []
    n = len(content)
    i = 0
    while True:
        start = content.find("nodes", i)
        if start == -1:
            break
        j = start + len("nodes")
        while j < n and content[j] in " \t\r\n":
            j += 1
        if j >= n or content[j] != "{":
            i = start + 1
            continue
        depth = 0
        k = j
        while k < n:
            if content[k] == "{":
                depth += 1
            elif content[k] == "}":
                depth -= 1
                if depth == 0:
                    break
            k += 1
        block = content[j + 1:k]
        node = {}
        uid = re.search(r"unique_id:\s*(\d+)", block)
        if uid:
            node["unique_id"] = int(uid.group(1))
        bm = re.search(r"bounds_in_screen\s*\{(.*?)\}", block, re.DOTALL)
        if bm:
            bs = bm.group(1)
            g = lambda key: re.search(rf"{key}:\s*(\d+)", bs)
            left = g("left").group(1) if g("left") else 0
            top = g("top").group(1) if g("top") else 0
            right = g("right").group(1) if g("right") else None
            bottom = g("bottom").group(1) if g("bottom") else None
            if right is not None and bottom is not None:
                node["bounds"] = {
                    "left": int(left), "top": int(top),
                    "width": int(right) - int(left),
                    "height": int(bottom) - int(top),
                }
        s = lambda key: re.search(rf'{key}:\s*"([^"]*)"', block)
        node["class_name"] = s("class_name").group(1) if s("class_name") else ""
        node["content_description"] = s("content_description").group(1) if s("content_description") else ""
        node["view_id_resource_name"] = s("view_id_resource_name").group(1) if s("view_id_resource_name") else ""
        node["text"] = s("text").group(1) if s("text") else ""
        node["is_clickable"] = "is_clickable: true" in block
        nodes.append(node)
        i = k + 1
    return nodes


def parse_control_info(action_str):
    m = re.search(r"\[(.*)\]\s*$", action_str)
    if not m:
        return {}
    inner = m.group(1)
    ctrl = {}
    km = re.search(r"class=([^\s,]+)", inner)
    if km:
        ctrl["class"] = km.group(1)
    km = re.search(r"content_desc='([^']*)'", inner)
    if km:
        ctrl["content_desc"] = km.group(1)
    km = re.search(r"resource_id=([^\s,]+)", inner)
    if km:
        ctrl["resource_id"] = km.group(1)
    km = re.search(r"text='([^']*)'", inner)
    if km:
        ctrl["text"] = km.group(1)
    return ctrl


def find_action_node(nodes, ctrl):
    if not nodes:
        return None
    cd = (ctrl.get("content_desc") or "").strip()
    rid = (ctrl.get("resource_id") or "").strip()
    txt = (ctrl.get("text") or "").strip()
    cls = (ctrl.get("class") or "").strip()
    best = None
    best_score = -1
    for node in nodes:
        score = 0
        if cd and node.get("content_description") == cd:
            score += 3
        if rid and node.get("view_id_resource_name") == rid:
            score += 3
        if txt and node.get("text") == txt:
            score += 2
        if cls and node.get("class_name") == cls:
            score += 1
        if score > best_score and node.get("bounds"):
            best_score = score
            best = node
    if best_score >= 2:
        return best
    return None


def extract_action_bounds(xml_path, ctrl):
    if not ctrl:
        return None
    nodes = parse_xml_nodes(xml_path)
    node = find_action_node(nodes, ctrl)
    return node.get("bounds") if node else None


# ================= 动作签名解析 =================

def parse_action_signature(action_str):
    m_type = re.search(r"action_type='([^']+)'", str(action_str))
    if not m_type:
        return "unknown"
    a_type = m_type.group(1)
    if a_type == "status":
        m_st = re.search(r"goal_status='([^']+)'", str(action_str))
        return f"status@{m_st.group(1)}" if m_st else "status"
    if a_type == "answer":
        m_ans = re.search(r"answer='([^']+)'", str(action_str))
        return f"answer@{m_ans.group(1)}" if m_ans else "answer"
    if a_type in ("scroll", "swipe"):
        m_dir = re.search(r"direction='([^']+)'", str(action_str))
        dir_s = f"_{m_dir.group(1)}" if m_dir else ""
        m_idx = re.search(r"index=(\d+)", str(action_str))
        idx_s = f"@{m_idx.group(1)}" if m_idx else ""
        return f"{a_type}{dir_s}{idx_s}"
    m_idx = re.search(r"index=(\d+)", str(action_str))
    return f"{a_type}@{m_idx.group(1)}" if m_idx else a_type


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
    m_text = re.search(r"text='([^']*)'", action_str)
    if m_text:
        action_data["text"] = m_text.group(1)
    return action_data


def get_image_src(rel_path):
    return rel_path.replace("\\", "/")


# ================= 组件级动作签名（用于轨迹一致性） =================

def get_component_from_xml(xml_path, index=None, bracket_info=None):
    """从 XML 文件中获取组件的稳定位信息。
    - 优先匹配 bracket_info 中的 content_desc/resource_id/text
    - 若匹配不到，用 index 找 unique_id=index 的节点
    - 返回: (class, content_desc, resource_id, text) 规范化字符串
    """
    if not os.path.exists(xml_path):
        return ""
    nodes = parse_xml_nodes(xml_path)
    if not nodes:
        return ""
    
    ctrl = parse_control_info(str(bracket_info) if bracket_info else "")
    cd = (ctrl.get("content_desc") or "").strip()
    rid = (ctrl.get("resource_id") or "").strip()
    txt = (ctrl.get("text") or "").strip()
    cls = (ctrl.get("class") or "").strip()
    
    # 尝试从 XML 中匹配 bracket_info 描述的组件
    if cd or rid or txt or cls:
        for node in nodes:
            node_cd = node.get("content_description", "")
            node_rid = node.get("view_id_resource_name", "")
            node_txt = node.get("text", "")
            node_cls = node.get("class_name", "")
            if ((cd and node_cd == cd) or (rid and node_rid == rid) or 
                (txt and node_txt == txt) or (cls and node_cls == cls)):
                name_parts = [node_cls, node_cd, node_rid, node_txt]
                return "|".join(p for p in name_parts if p).lower()
    
    # 退化为用 index 找 unique_id 对应的节点
    if index is not None:
        for node in nodes:
            if node.get("unique_id") == index:
                name_parts = [node.get("class_name", ""), node.get("content_description", ""), 
                              node.get("view_id_resource_name", ""), node.get("text", "")]
                return "|".join(p for p in name_parts if p).lower()
    
    # 最后退回 bracket 提取的 name
    name = (cd or rid or txt or "").strip().lower()
    return name


def component_name_of(step, xml_path=None):
    """获取被操作组件的“稳定名字”，用于确认点击的是同一个组件。"""
    action_field = str(step.get("action", step.get("model_output_action", "")))
    ctrl = parse_control_info(action_field)
    
    # 如果有 XML，优先从 XML 验证组件身份
    if xml_path:
        m_idx = re.search(r"index=(\d+)", str(step.get("model_output_action", "")))
        idx = int(m_idx.group(1)) if m_idx else None
        xml_name = get_component_from_xml(xml_path, index=idx, bracket_info=ctrl)
        if xml_name:
            return xml_name
    
    # 退化为从 action bracket 提取
    name = (ctrl.get("content_desc") or ctrl.get("resource_id") or ctrl.get("text") or "").strip().lower()
    return name


def build_step_signature(step, xml_path=None):
    """构造一步的动作签名。
    签名 = action_type + index + component_identity
    component_identity 来自 XML 或 action bracket，确保不同 XML 屏幕
    点击相同 index 时不会误判为同一组件。
    """
    act = str(step.get("model_output_action", ""))
    action_field = str(step.get("action", act) or act)
    m_type = re.search(r"action_type='([^']+)'", act)
    if not m_type:
        return "unknown"
    a_type = m_type.group(1)

    if a_type == "status":
        m_st = re.search(r"goal_status='([^']+)'", act)
        return f"status@{m_st.group(1)}" if m_st else "status"
    if a_type == "answer":
        m_ans = re.search(r"answer='([^']*)'", act)
        return f"answer@{m_ans.group(1)}" if m_ans else "answer"
    if a_type in ("scroll", "swipe"):
        m_dir = re.search(r"direction='([^']+)'", act)
        dir_s = f"_{m_dir.group(1)}" if m_dir else ""
        m_idx = re.search(r"index=(\d+)", act)
        idx_s = f"@{m_idx.group(1)}" if m_idx else ""
        return f"{a_type}{dir_s}{idx_s}"

    m_idx = re.search(r"\bindex=(\d+)", act)
    idx_s = m_idx.group(1) if m_idx else ""
    name = component_name_of(step, xml_path)

    if a_type == "input_text":
        m_text = re.search(r"text='([^']*)'", act)
        text_s = m_text.group(1) if m_text else ""
        return f"{a_type}@{idx_s}#{name}#{text_s}"
    # click / long_press 等带组件的动作：组件号 + 组件名
    return f"{a_type}@{idx_s}#{name}"


def compress_sequence(seq):
    """把连续的 scroll / swipe / wait 轨迹压缩成一次。
    - scroll, swipe, wait 都归一为 SCROLL_WAIT
    - 连续的 SCROLL_WAIT 合并为一个
    - scroll 和 swipe 连续也算作一类（因为都属于滚动行为）
    """
    norm = []
    for tok in seq:
        if tok.startswith("scroll") or tok.startswith("swipe") or tok.startswith("wait"):
            norm.append("SCROLL_WAIT")
        else:
            norm.append(tok)
    out = []
    for t in norm:
        if out and out[-1] == "SCROLL_WAIT" and t == "SCROLL_WAIT":
            continue
        out.append(t)
    return tuple(out)


def get_trace_info(trace_path):
    try:
        with open(trace_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not data:
            return None, None, None, 0, 0, False

        data_list = data if isinstance(data, list) else [data]
        first_step = data_list[0]
        goal = str(first_step.get("goal", "")).strip()
        task_id = str(first_step.get("task_id", "")).strip()

        run_path = os.path.dirname(trace_path)
        raw_actions = [step.get("model_output_action", "") for step in data_list if "model_output_action" in step]
        
        # 为每一步构建签名，传入对应的 XML 路径进行组件身份校验
        parsed_actions = []
        for i, step in enumerate(data_list):
            xml_path = os.path.join(run_path, "xmls", f"{i}_before.xml")
            parsed_actions.append(build_step_signature(step, xml_path))
        
        # 连续 scroll/wait 压缩为一次，再用于一致性比较
        compressed_actions = compress_sequence(parsed_actions)

        unknown_count = sum(1 for act in parsed_actions if act == "unknown")
        valid_actions = [act for act in parsed_actions if act != "unknown"]

        last_raw_action = str(raw_actions[-1]) if raw_actions else ""
        is_completed = "action_type='status'" in last_raw_action and "goal_status='complete'" in last_raw_action

        return task_id, goal, compressed_actions, len(raw_actions), unknown_count, is_completed
    except Exception as e:
        print(f"读取 {trace_path} 出错: {e}")
        return None, None, None, 0, 0, False


def levenshtein(s1, s2):
    if len(s1) < len(s2):
        return levenshtein(s2, s1)
    if len(s2) == 0:
        return len(s1)
    prev = list(range(len(s2) + 1))
    for i, c1 in enumerate(s1):
        curr = [i + 1]
        for j, c2 in enumerate(s2):
            ins = prev[j + 1] + 1
            dele = curr[j] + 1
            sub = prev[j] + (0 if c1 == c2 else 1)
            curr.append(min(ins, dele, sub))
        prev = curr
    return prev[-1]


def normalized_levenshtein(s1, s2):
    if not s1 and not s2:
        return 0.0
    ml = max(len(s1), len(s2))
    return 0.0 if ml == 0 else levenshtein(s1, s2) / ml


def get_consistency_type_v2(sequences, thresholds=(0.0, 0.15, 0.35)):
    if not sequences or len(sequences) < 2:
        return -1, {}
    n = len(sequences)
    dists = []
    for i in range(n):
        for j in range(i + 1, n):
            dists.append(normalized_levenshtein(sequences[i], sequences[j]))
    if not dists:
        return -1, {}
    mx = max(dists)
    mn = sum(dists) / len(dists)
    sc = Counter(sequences)
    mr = sc.most_common(1)[0][1] / n
    metrics = {
        "n": n, "mean_distance": round(mn, 4), "max_distance": round(mx, 4),
        "std_distance": round((sum((d - mn) ** 2 for d in dists) / len(dists)) ** 0.5, 4),
        "majority_ratio": round(mr, 4),
    }

    st, nt, mt = thresholds
    if mx == 0.0:
        return 0, metrics

    if n >= 3:
        sc2 = Counter(sequences)
        mc2_seq, mc2_count = sc2.most_common(1)[0]
        if mc2_count >= (n * 2 + 2) // 3:
            return 1, metrics

    if mx <= nt:
        return 1, metrics
    if mx <= mt:
        return 2, metrics
    return 3, metrics


def group_traces_by_task_id(all_traces):
    groups = defaultdict(list)
    for t in all_traces:
        groups[t["task_id"]].append(t)
    return [groups[k] for k in sorted(groups.keys())]


# ================= 轨迹批量分析 =================

def analyze_trajectories(root_dir):
    exp_dirs = [d for d in os.listdir(root_dir) if os.path.isdir(os.path.join(root_dir, d))
                and re.match(r"outputs-", d)]

    def sort_key(name):
        temp_match = re.search(r"outputs-T([\d.]+)", name)
        temp_val = float(temp_match.group(1)) if temp_match else 0.0
        is_noseed = 1 if "noseed" in name else 0
        return (temp_val, is_noseed)

    exp_dirs.sort(key=sort_key)

    report_data = {}

    for exp_dir in exp_dirs:
        exp_path = os.path.join(root_dir, exp_dir)

        all_traces = []
        total_unknown_actions = 0
        for run_dir in os.listdir(exp_path):
            if run_dir == "logs":
                continue
            run_path = os.path.join(exp_path, run_dir)
            if not os.path.isdir(run_path):
                continue
            trace_file = os.path.join(run_path, "trace.json")
            if not os.path.exists(trace_file):
                continue

            task_id, goal, actions, raw_steps, unk_count, is_comp = get_trace_info(trace_file)
            if goal and actions is not None:
                total_unknown_actions += unk_count
                all_traces.append({
                    "id": run_dir,
                    "task_id": task_id,
                    "goal": goal,
                    "actions": actions,
                    "raw_steps": raw_steps,
                    "is_completed": is_comp,
                })

        clusters = group_traces_by_task_id(all_traces)
        multi_run_clusters = [c for c in clusters if len(c) > 1]

        strict_consistent = []
        similar = []
        moderate = []
        divergent = []
        task_all_completed = 0

        for cluster in multi_run_clusters:
            task_id = cluster[0]["task_id"]
            seqs = [t["actions"] for t in cluster]
            ctype, cmetrics = get_consistency_type_v2(seqs)

            is_all_completed = all(t["is_completed"] for t in cluster)
            if is_all_completed:
                task_all_completed += 1

            sample_goal = cluster[0]["goal"]
            seq_counter = Counter(seqs)
            representative_seq = seq_counter.most_common(1)[0][0]
            step_counts = [t["raw_steps"] for t in cluster]

            info = (task_id, len(cluster), sample_goal, representative_seq,
                    [t["id"] for t in cluster], is_all_completed, step_counts)

            if ctype == 0:
                strict_consistent.append(info)
            elif ctype == 1:
                similar.append(info)
            elif ctype == 2:
                moderate.append(info)
            elif ctype >= 3:
                divergent.append(info)

        trace_consistency_map = {}
        for _ctype, _clusters in [(0, strict_consistent), (1, similar), (2, moderate), (3, divergent)]:
            _clabel = CONSISTENCY_LABELS[_ctype]
            for _info in _clusters:
                for _tid in _info[4]:
                    trace_consistency_map[_tid] = {"type": _ctype, "label": _clabel}
        report_data[exp_dir] = {
            "total_unique_tasks": len(set(t["task_id"] for t in all_traces)),
            "multi_run_count": len(multi_run_clusters),
            "task_all_completed": task_all_completed,
            "total_unknown_actions": total_unknown_actions,
            "strict": strict_consistent,
            "similar": similar,
            "moderate": moderate,
            "divergent": divergent,
            "trace_consistency_map": trace_consistency_map,
        }

    return report_data


def generate_outputs(report_data):
    with open("unknown_actions_report.txt", "w", encoding="utf-8") as fu:
        fu.write("============= 未知动作统计报告 =============\n\n")
        for exp_dir, data in report_data.items():
            fu.write(f"{exp_dir:<35} -> 未知动作总数: {data['total_unknown_actions']}\n")

    with open("trajectory_analysis_report.txt", "w", encoding="utf-8") as f:
        f.write("轨迹一致性分析报告\n")
        f.write("=" * 70 + "\n\n")
        f.write("分类规则说明:\n")
        f.write("  完全一致(Strict): 所有轨迹的折叠动作序列完全相同\n")
        f.write("  高度相似 (Similar):\n")
        f.write("    (a) >=3条轨迹时，n-1条一致+1条有偏差（差异步数<=3）\n")
        f.write("    (b) 所有两两比较差异步数<=3\n")
        f.write("  严重分歧 (Divergent): 不满足以上条件\n")
        f.write("  差异步数 = 不同步数 + 长度差\n\n")

        for exp_dir, data in report_data.items():
            f.write(f"{'=' * 70}\n")
            f.write(f"实验组 {exp_dir}\n")
            f.write(f"  任务模板类型总数: {data['total_unique_tasks']}\n")
            f.write(f"  实验组数 (按 task_id 分组): {data['multi_run_count']}\n")
            f.write(f"  全部运行完成的实验组数: {data['task_all_completed']}\n")
            f.write(f"  未知动作总数: {data['total_unknown_actions']}\n")
            f.write(f"{'-' * 70}\n")

            strict_comp = sum(1 for info in data["strict"] if info[5])
            sim_comp = sum(1 for info in data["similar"] if info[5])
            mod_comp = sum(1 for info in data["moderate"] if info[5])
            div_comp = sum(1 for info in data["divergent"] if info[5])

            f.write(f"\n--- 完全一致({len(data['strict'])} ? 全部完成 {strict_comp} ? ---\n")
            for task_id, r_count, goal, seq, dirs, all_comp, step_counts in data["strict"]:
                f.write(f"  [{task_id}] ({r_count} 次运行 {'[全部完成]' if all_comp else '[部分完成]'}\n")
                f.write(f"    目标: {goal}\n")
                f.write(f"    动作序列: {' -> '.join(seq)}\n")
                f.write(f"    各轨迹步数 {step_counts}\n")

            f.write(f"\n--- 高度相似 ({len(data['similar'])} ? 全部完成 {sim_comp} ? ---\n")
            for task_id, r_count, goal, seq, dirs, all_comp, step_counts in data["similar"]:
                f.write(f"  [{task_id}] ({r_count} 次运行 {'[全部完成]' if all_comp else '[部分完成]'}\n")
                f.write(f"    目标: {goal}\n")
                f.write(f"    代表动作序列: {' -> '.join(seq)}\n")
                f.write(f"    各轨迹步数 {step_counts}\n")
                f.write(f"    轨迹目录:\n")
                for i, d in enumerate(dirs):
                    f.write(f"      [{i+1}] ...{d[-30:]}\n")

            f.write(f"\n--- 中等相似 ({len(data['moderate'])} ? 全部完成 {mod_comp} ? ---\n")
            for task_id, r_count, goal, seq, dirs, all_comp, step_counts in data["moderate"]:
                f.write(f"  [{task_id}] ({r_count} 次运行 {'[全部完成]' if all_comp else '[部分完成]'}\n")
                f.write(f"    目标: {goal}\n")
                f.write(f"    各轨迹步数 {step_counts}\n")
                f.write(f"    轨迹目录:\n")
                for i, d in enumerate(dirs):
                    f.write(f"      [{i+1}] ...{d[-30:]}\n")

            f.write(f"\n--- 严重分歧 ({len(data['divergent'])} ? 全部完成 {div_comp} ? ---\n")
            for task_id, r_count, goal, seq, dirs, all_comp, step_counts in data["divergent"]:
                f.write(f"  [{task_id}] ({r_count} 次运行 {'[全部完成]' if all_comp else '[部分完成]'}\n")
                f.write(f"    目标: {goal}\n")
                f.write(f"    各轨迹步数 {step_counts}\n")

            f.write("\n\n")

    plot_list = []
    for exp_dir, data in report_data.items():
        parts = exp_dir.split("-")
        line1 = parts[0]
        temp_part = parts[1] if len(parts) > 1 else ""
        if "noseed" in temp_part or "Fix" in temp_part:
            line2 = temp_part.replace("noseed", "").replace("FixedSeed", "")
            line3 = "固定参数 (无随机种子)"
        else:
            line2 = temp_part
            line3 = "随机参数 (有随机种子)"
        label = f"{line1}\n{line2}\n{line3}"

        strict_total = len(data["strict"])
        strict_comp = sum(1 for info in data["strict"] if info[5])
        sim_total = len(data["similar"])
        sim_comp = sum(1 for info in data["similar"] if info[5])
        mod_total = len(data["moderate"])
        mod_comp = sum(1 for info in data["moderate"] if info[5])
        sm_total = sim_total + mod_total
        div_total = len(data["divergent"])
        div_comp = sum(1 for info in data["divergent"] if info[5])

        plot_list.append({
            "Label": label,
            "strict_total": strict_total,
            "strict_comp": strict_comp,
            "sim_total": sim_total,
            "sim_comp": sim_comp,
            "mod_total": mod_total,
            "mod_comp": mod_comp,
            "sm_total": sm_total,
            "div_total": div_total,
            "div_comp": div_comp,
            "task_all_completed": data["task_all_completed"],
            "total_participating": data["multi_run_count"],
        })

    df = pd.DataFrame(plot_list)
    if df.empty:
        return

    fig, ax1 = plt.subplots(figsize=(18, 9))
    x = np.arange(len(df))
    width = 0.18

    pos_strict = x - 0.27
    pos_sim_mod = x - 0.09
    pos_div = x + 0.09
    pos_all = x + 0.27

    rects1 = ax1.bar(pos_strict, df["strict_total"], width,
                     label="完全一致(Strict) 总数", color="#2ecc71")
    rects2_bottom = ax1.bar(pos_sim_mod, df["sim_total"], width,
                            label="高度相似 (Similar) 总数", color="#f1c40f")
    rects2_top = ax1.bar(pos_sim_mod, df["mod_total"], width,
                         bottom=df["sim_total"].values,
                         label="中等相似 (Moderate)", color="#9b59b6")
    rects4 = ax1.bar(pos_div, df["div_total"], width,
                     label="严重分歧 (Divergent) 总数", color="#e74c3c")

    rects4_s = ax1.bar(pos_all, df["strict_comp"], width, color="#27ae60",
                       label="全部完成-完全一致")
    rects4_m = ax1.bar(pos_all, df["sim_comp"], width, bottom=df["strict_comp"].values, color="#d4ac0d",
                       label="全部完成-高度相似")
    rects4_mod = ax1.bar(pos_all, df["mod_comp"], width,
                        bottom=df["strict_comp"].values + df["sim_comp"].values, color="#8e44ad",
                        label="全部完成-中等相似")
    rects4_d = ax1.bar(pos_all, df["div_comp"], width,
                       bottom=df["strict_comp"].values + df["sim_comp"].values + df["mod_comp"].values, color="#c0392b",
                       label="全部完成-严重分歧")

    ax1.set_ylabel("实验组数量", fontsize=14, fontweight="bold")
    ax1.set_title("轨迹一致性分析 (Trajectory Consistency Analysis)", fontsize=18, fontweight="bold", pad=25)
    ax1.set_xticks(x)
    ax1.set_xticklabels(df["Label"], fontsize=9.5)

    for i in range(len(df) - 1):
        ax1.axvline(x=i + 0.5, color='gray', linestyle='--', linewidth=0.8, alpha=0.5)

    ax1.legend(fontsize=10, loc="upper left", bbox_to_anchor=(1, 1))
    ax1.yaxis.set_major_locator(MaxNLocator(integer=True))

    def autolabel(containers):
        for container in containers:
            for rect in container:
                h = rect.get_height()
                if h > 0:
                    ax1.annotate(str(int(h)),
                                 xy=(rect.get_x() + rect.get_width() / 2, rect.get_y() + h / 2),
                                 xytext=(0, 0), textcoords="offset points",
                                 ha="center", va="center", fontweight="bold", fontsize=11,
                                 color="white" if h > 1.5 else "#333")

    autolabel([rects1, rects2_bottom, rects2_top, rects4])

    for i in range(len(df)):
        total_groups = df.iloc[i]["total_participating"]
        if total_groups <= 0:
            total_groups = df.iloc[i]["strict_total"] + df.iloc[i]["sm_total"] + df.iloc[i]["div_total"]

        cols = ["strict_total", "sm_total", "div_total"]
        rects_list = [rects1, rects2_top, rects4]
        pcts = []
        for col in cols:
            val = df.iloc[i][col]
            pcts.append(round(val / total_groups * 100) if total_groups > 0 else 0)

        total_pct = sum(pcts)
        if total_pct != 100 and total_pct > 0:
            max_idx = max(range(len(pcts)), key=lambda j: pcts[j])
            pcts[max_idx] += (100 - total_pct)

        for j, (rects, col) in enumerate(zip(rects_list, cols)):
            rect = rects[i]
            h = rect.get_height()
            if col == "sm_total":
                h = df.iloc[i]["sim_total"] + df.iloc[i]["mod_total"]
            val = df.iloc[i][col]
            if h > 0 and total_groups > 0 and val > 0:
                ax1.annotate(f"{pcts[j]}%",
                             xy=(rect.get_x() + rect.get_width() / 2, h),
                             xytext=(0, 5),
                             textcoords="offset points",
                             ha="center", va="bottom", fontweight="bold", fontsize=9,
                             color="#555")

    for idx, (rect, val) in enumerate(zip(rects4_s, df["strict_comp"])):
        if val > 0:
            ax1.annotate(str(int(val)),
                        xy=(rect.get_x() + rect.get_width() / 2, rect.get_y() + rect.get_height() / 2),
                        xytext=(0, 0), textcoords="offset points",
                        ha="center", va="center", fontweight="bold", fontsize=10, color="white")
    for idx, (rect, val) in enumerate(zip(rects4_m, df["sim_comp"])):
        if val > 0:
            ax1.annotate(str(int(val)),
                        xy=(rect.get_x() + rect.get_width() / 2, rect.get_y() + rect.get_height() / 2),
                        xytext=(0, 0), textcoords="offset points",
                        ha="center", va="center", fontweight="bold", fontsize=10, color="#333")
    for idx, (rect, val) in enumerate(zip(rects4_mod, df["mod_comp"])):
        if val > 0:
            ax1.annotate(str(int(val)),
                        xy=(rect.get_x() + rect.get_width() / 2, rect.get_y() + rect.get_height() / 2),
                        xytext=(0, 0), textcoords="offset points",
                        ha="center", va="center", fontweight="bold", fontsize=10, color="white")
    for idx, (rect, val) in enumerate(zip(rects4_d, df["div_comp"])):
        if val > 0:
            ax1.annotate(str(int(val)),
                        xy=(rect.get_x() + rect.get_width() / 2, rect.get_y() + rect.get_height() / 2),
                        xytext=(0, 0), textcoords="offset points",
                        ha="center", va="center", fontweight="bold", fontsize=10, color="white")

    plt.tight_layout()
    plt.savefig("trajectory_consistency_chart.png", dpi=300)
    print("图表和报告已成功生成")
    print("  -> trajectory_consistency_chart.png")
    print("  -> trajectory_analysis_report.txt")
    print("  -> unknown_actions_report.txt")

    consistency_json = {}
    for exp_dir, data in report_data.items():
        consistency_json[exp_dir] = data.get("trace_consistency_map", {})
    json_path = "trajectory_consistency.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(consistency_json, f, ensure_ascii=False, indent=2)
    print(f"  -> {json_path}")


# ================= HTML 可视化 =================

CSS = """* { box-sizing: border-box; margin: 0; padding: 0; }
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
  .step-images { cursor: zoom-in; }
  .lightbox { display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.92); z-index: 1000; justify-content: center; align-items: center; flex-wrap: wrap; gap: 24px; padding: 40px; cursor: zoom-out; }
  .lightbox .lb-img { display: block; max-width: 46vw; max-height: 88vh; border-radius: 6px; border: 1px solid #555; box-shadow: 0 0 30px rgba(0,0,0,0.8); }
  .lightbox .lb-wrap { position: relative; display: inline-block; transform-origin: center center; }
  .img-wrapper { position: relative; display: inline-block; }
  .img-wrapper .overlays { position: absolute; top: 0; left: 0; width: 100%; height: 100%; pointer-events: none; overflow: hidden; }
  .lightbox .lb-close { position: fixed; top: 18px; right: 26px; color: #fff; font-size: 32px; font-weight: bold; cursor: pointer; z-index: 1001; line-height: 1; }
  .lightbox .lb-hint { position: fixed; bottom: 18px; left: 0; right: 0; text-align: center; color: #bbb; font-size: 0.85em; }
"""

LIGHTBOX_JS = """
function openLightbox(container) {
  const imgs = container.querySelectorAll('.img-wrapper img');
  const lb = document.getElementById('lightbox');
  const closeBtn = lb.querySelector('.lb-close');
  const hint = lb.querySelector('.lb-hint');
  closeBtn.style.display = 'block';
  hint.style.display = 'block';
  while (lb.children.length > 2) {
    lb.removeChild(lb.lastChild);
  }
  const actionStr = (container.closest('[data-action]') || container.querySelector('[data-action]'));
  const actionAttr = actionStr ? actionStr.getAttribute('data-action') : '';
  imgs.forEach(function(img, idx) {
    const wrap = document.createElement('div');
    wrap.className = 'img-wrapper lb-wrap';
    wrap._s = 1;
    const big = document.createElement('img');
    big.className = 'lb-img';
    big.src = img.src;
    if (idx === 0 && actionAttr) big.setAttribute('data-action', actionAttr);
    const overlays = document.createElement('div');
    overlays.className = 'overlays';
    wrap.appendChild(big);
    wrap.appendChild(overlays);
    big.addEventListener('load', function() { updateOverlay(big); });
    wrap.addEventListener('wheel', function(e) {
      e.preventDefault();
      e.stopPropagation();
      wrap._s = Math.min(4, Math.max(0.3, wrap._s - (e.deltaY > 0 ? -0.1 : 0.1)));
      wrap.style.transform = 'scale(' + wrap._s + ')';
    }, { passive: false });
    wrap.addEventListener('dblclick', function(e) {
      e.stopPropagation();
      wrap._s = wrap._s > 1 ? 1 : 2;
      wrap.style.transform = 'scale(' + wrap._s + ')';
    });
    lb.appendChild(wrap);
    if (big.complete) updateOverlay(big);
  });
  lb.style.display = 'flex';
}

function closeLightbox(e) {
  if (e && e.target && e.target.classList && e.target.classList.contains('lb-wrap')) return;
  const lb = document.getElementById('lightbox');
  lb.style.display = 'none';
  while (lb.children.length > 2) {
    lb.removeChild(lb.lastChild);
  }
}

document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') {
    const lb = document.getElementById('lightbox');
    if (lb.style.display === 'flex') closeLightbox();
  }
});
"""

JS = """let currentGroupId = __FIRST_GROUP__;

function switchGroup(groupName) {
  const items = document.querySelectorAll('.group-item');
  let targetIdx = -1;
  items.forEach((item, idx) => {
    if (item.id === 'menu-' + groupName) {
      targetIdx = idx;
    }
  });
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
  if (display) {
    display.querySelectorAll('.before-img').forEach(img => {
      if (img.complete) {
        updateOverlay(img);
      }
    });
  }
}

function updateOverlay(img) {
  const stepRow = img.closest('[data-action]');
  const overlays = img.parentElement.querySelector('.overlays');
  if (!stepRow || !overlays) return;

  overlays.innerHTML = '';

  const actionStr = stepRow.getAttribute('data-action');
  if (!actionStr || actionStr === 'null' || actionStr === '') return;

  let action;
  try { action = JSON.parse(actionStr); } catch(e) { return; }

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

  if (action.bounds) {
    let box = document.createElement('div');
    box.className = 'action-box-overlay';
    box.style.left = (offsetX + action.bounds.left * scale) + 'px';
    box.style.top = (offsetY + action.bounds.top * scale) + 'px';
    box.style.width = (action.bounds.width * scale) + 'px';
    box.style.height = (action.bounds.height * scale) + 'px';
    overlays.appendChild(box);
  }

  if (!action.bounds && action.x !== undefined && action.y !== undefined) {
    let dot = document.createElement('div');
    dot.className = 'action-dot-overlay';
    dot.style.left = (offsetX + action.x * scale) + 'px';
    dot.style.top = (offsetY + action.y * scale) + 'px';
    overlays.appendChild(dot);
  }

  if (action.type === 'scroll' || action.type === 'swipe') {
    if (action.direction) {
      let arrow = document.createElement('div');
      arrow.className = 'action-arrow-overlay ' + action.direction;
      const dirCN = {'up': '上', 'down': '下', 'left': '左', 'right': '右'};
      const dirLabel = dirCN[action.direction] || action.direction;
      const actionCN = action.type === 'scroll' ? '滚动' : '滑动';
      arrow.innerHTML = '<span>' + actionCN + ': ' + dirLabel + '</span>';
      overlays.appendChild(arrow);
    }
  }

  if (action.type === 'input_text' && action.text) {
    let textLabel = document.createElement('div');
    textLabel.className = 'action-text-overlay';
    textLabel.innerText = '输入: ' + action.text;
    if (action.bounds) {
      textLabel.style.left = (offsetX + action.bounds.left * scale) + 'px';
      textLabel.style.top = Math.max(0, offsetY + action.bounds.top * scale - 30) + 'px';
    } else if (action.x !== undefined && action.y !== undefined) {
      textLabel.style.left = (offsetX + action.x * scale) + 'px';
      textLabel.style.top = Math.max(0, offsetY + action.y * scale - 30) + 'px';
    } else {
      textLabel.style.left = '50%';
      textLabel.style.top = '10%';
      textLabel.style.transform = 'translateX(-50%)';
    }
    overlays.appendChild(textLabel);
  }
}

let resizeTimer;
window.addEventListener('resize', () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => {
    const display = document.getElementById(currentGroupId);
    if (display) {
      display.querySelectorAll('.before-img').forEach(img => {
        if (img.complete) updateOverlay(img);
      });
    }
  }, 200);
});

document.getElementById('searchInput').addEventListener('input', function() {
  const filter = this.value.toLowerCase();
  const items = document.querySelectorAll('.group-item');
  let firstVisible = null;
  items.forEach(function(item) {
    const name = item.querySelector('.group-name').textContent.toLowerCase();
    const goal = item.querySelector('.group-goal').textContent.toLowerCase();
    const match = name.includes(filter) || goal.includes(filter);
    item.style.display = match ? '' : 'none';
    if (match && !firstVisible) firstVisible = item;
  });
  const activeItem = document.querySelector('.group-item.active');
  if (activeItem && activeItem.style.display === 'none' && firstVisible) {
    const groupName = firstVisible.id.replace('menu-', '');
    switchGroup(groupName);
  }
});
"""


def generate_grouped_report(outputs_dir, consistency_map=None):
    if consistency_map is None:
        consistency_map = {}

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
        task_id = trace_log[0].get("task_id", run_dir)
        all_traces.append({
            "id": run_dir,
            "task_id": task_id,
            "goal": goal,
            "steps": trace_log,
        })

    if not all_traces:
        print(f"在 {outputs_dir} 中未找到有效轨迹")
        return

    task_groups = group_traces_by_task_id(all_traces)
    total_groups = len(task_groups)
    total_runs = len(all_traces)

    html_parts = []
    html_parts.append(f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Android World - 分组轨迹对比</title>
<style>
{CSS}</style>
</head>
<body>

<div class="sidebar">
  <div class="sidebar-header">
    任务分组 (按 task_id)
    <small>{total_groups} 组, {total_runs} 条轨迹</small>
  </div>
  <div class="search-box">
    <input type="text" id="searchInput" placeholder="筛选...">
  </div>
  <div class="group-list" id="groupList">
""")

    for gi, group in enumerate(task_groups):
        task_id = group[0]["task_id"]
        sample_goal = escape(group[0]["goal"])
        active_attr = "active" if gi == 0 else ""
        safe_name = task_id.replace("'", "\\'").replace('"', "&quot;")
        safe_id = f"{task_id}_{gi}".replace("'", "\\'").replace('"', "&quot;")
        group_consistency = None
        for trace_obj in group:
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
      <div class="group-name">{escape(task_id)}</div>
      <div class="group-meta">任务组 #{gi+1} | {len(group)} 条轨迹</div>
      {consistency_html}
      <div class="group-goal">{sample_goal[:100]}{'...' if len(sample_goal) > 100 else ''}</div>
    </div>
""")

    html_parts.append("""  </div>
</div>

<div class="main-content" id="mainContent">
""")

    first_group_id = None
    for gi, group in enumerate(task_groups):
        group_id = f"grp-{gi}"
        if gi == 0:
            first_group_id = group_id
        task_id = group[0]["task_id"]
        sample_goal = escape(group[0]["goal"])
        active_cls = "active" if gi == 0 else ""

        gc = None
        for trace_obj in group:
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
      <div class="group-title">{escape(task_id)}</div>
      <div class="group-goal">目标: {sample_goal}</div>
      <div class="group-stats">任务组 #{gi+1} | {len(group)} 条轨迹</div>
      {consistency_main_html}
    </div>
    <div class="runs-scroll">
""")

        for trace in group:
            steps = trace["steps"]
            n_steps = len(steps)
            last_action = str(steps[-1].get("model_output_action", ""))
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

                action_str = str(step.get("model_output_action", ""))
                action_data = parse_action_str(action_str)
                raw_action_str = str(step.get("action", action_str))
                ctrl = parse_control_info(raw_action_str)

                if ctrl:
                    xml_path = os.path.join(outputs_dir, trace["id"], "xmls", f"{si}_before.xml")
                    action_bounds = extract_action_bounds(xml_path, ctrl)
                    if action_bounds:
                        action_data["bounds"] = action_bounds

                action_json_str = json.dumps(action_data).replace("'", "&#39;")
                action_type = escape(action_data.get("type", "unknown"))
                action_text = escape(raw_action_str)
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
          <div class="step-images" ondblclick="openLightbox(this)">
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

    html_parts.append(f"""</div>

<div class="lightbox" id="lightbox" onclick="closeLightbox(event)">
  <div class="lb-close" onclick="closeLightbox(event)">&times;</div>
  <div class="lb-hint">点击任意处关闭 · 双击图片可缩放</div>
</div>

<script>
{JS.replace('__FIRST_GROUP__', json.dumps(first_group_id if first_group_id else 'grp-0'))}
{LIGHTBOX_JS}
</script>

</body>
</html>
""")

    html_content = "".join(html_parts)
    report_path = os.path.join(outputs_dir, "grouped_report.html")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(html_content)
    print(f"分组轨迹报告已生成: {report_path}")


# ================= 索引页 =================

def generate_index(output_dirs, output_dir):
    html = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>轨迹分析 - 总索引</title>
<style>
  body { font-family: "Microsoft YaHei", sans-serif; background: #1e1e1e; color: #e0e0e0; max-width: 900px; margin: 40px auto; padding: 0 20px; }
  h1 { color: #4fc3f7; border-bottom: 2px solid #333; padding-bottom: 10px; }
  h2 { color: #81c784; margin-top: 30px; }
  .report-item { background: #252526; border-radius: 8px; padding: 16px 20px; margin: 12px 0; border-left: 4px solid #007acc; }
  .report-item h3 { margin: 0 0 6px 0; color: #e0e0e0; }
  .report-item a { color: #4fc3f7; text-decoration: none; }
  .report-item a:hover { text-decoration: underline; }
  .report-item .desc { color: #888; font-size: 0.9em; }
  .meta { color: #888; font-size: 0.85em; margin-top: 20px; }
  ul { list-style: none; padding: 0; }
  li { padding: 6px 0; }
  li a { color: #81c784; }
</style>
</head>
<body>
<h1>轨迹分析报告总索引</h1>
<p style="color:#aaa;">生成时间: """ + datetime.now().strftime("%Y-%m-%d %H:%M:%S") + """</p>
"""

    for d in output_dirs:
        name = os.path.basename(d)
        html += f'<div class="report-item">\n'
        html += f'  <h3>{name}</h3>\n'
        html += f'  <ul>\n'

        html_path = os.path.join(d, "grouped_report.html")
        if os.path.exists(html_path):
            rel = os.path.relpath(html_path, output_dir)
            html += f'    <li><a href="{rel}">分组轨迹对比 HTML 报告</a></li>\n'

        html += f'  </ul>\n'
        html += f'</div>\n'

    chart_path = os.path.join(output_dir, "trajectory_consistency_chart.png")
    if os.path.exists(chart_path):
        rel = os.path.relpath(chart_path, output_dir)
        html += f'<h2>全局图表</h2>\n'
        html += f'<p><a href="{rel}"><img src="{rel}" style="max-width:100%;border-radius:8px;"></a></p>\n'

    html += f'<div class="meta">'
    html += f'<p>工具文件: trace_analyzer.py</p>'
    html += f'<p>数据目录数: {len(output_dirs)}</p>'
    html += f'</div>\n'
    html += "</body></html>"

    index_path = os.path.join(output_dir, "index.html")
    with open(index_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"总索引页已生成: {index_path}")
    return index_path


# ================= 主入口 =================

def find_output_dirs(root_dir):
    dirs = []
    for entry in os.listdir(root_dir):
        full = os.path.join(root_dir, entry)
        if os.path.isdir(full) and entry.startswith("outputs-"):
            dirs.append(full)
    return sorted(dirs)


def main():
    parser = argparse.ArgumentParser(
        description="轨迹分析工具集 - 一致性分析 + 分组可视化 (单文件版)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python trace_analyzer.py
  python trace_analyzer.py --dir /path/to/data
  python trace_analyzer.py --dirs /path/to/dir1 /path/to/dir2
  python trace_analyzer.py --skip-html    # 跳过 HTML 生成（只做一致性分析）
  python trace_analyzer.py --skip-chart   # 跳过一致性分析（只做 HTML）
        """,
    )
    parser.add_argument("--dir", default=None, help="分析根目录（默认为当前目录）")
    parser.add_argument("--dirs", nargs="+", default=None, help="分析多个指定目录")
    parser.add_argument("--skip-chart", action="store_true", help="跳过一致性分析")
    parser.add_argument("--skip-html", action="store_true", help="跳过 HTML 生成")
    parser.add_argument("--skip-index", action="store_true", help="跳过索引页生成")
    args = parser.parse_args()

    if args.dirs:
        output_dirs = args.dirs
        root_dir = os.path.commonpath(output_dirs) if len(output_dirs) > 1 else os.path.dirname(output_dirs[0])
    elif args.dir:
        root_dir = args.dir
    else:
        root_dir = os.getcwd()

    root_dir = os.path.abspath(root_dir)
    print(f"{'='*60}")
    print(f"  轨迹分析工具集 (trace_analyzer.py)")
    print(f"{'='*60}")
    print(f"工作目录: {root_dir}")
    print()

    if not args.dirs:
        output_dirs = find_output_dirs(root_dir)
    if not output_dirs:
        print(f"[警告] 未找到 outputs-* 目录")
        sys.exit(1)

    print(f"发现 {len(output_dirs)} 个数据目录:")
    for d in output_dirs:
        print(f"  - {os.path.basename(d)}")
    print()

    consistency_data = None
    if not args.skip_chart:
        print("[1/2] 轨迹一致性分析...")
        data = analyze_trajectories(root_dir)
        for exp_dir, dd in data.items():
            print(f"{'='*60}")
            print(f"实验组 {exp_dir}")
            print(f"  完全一致: {len(dd['strict'])} | 高度相似: {len(dd['similar'])} | 中等相似: {len(dd['moderate'])} | 严重分歧: {len(dd['divergent'])}")
            print(f"  任务组数: {dd['total_unique_tasks']} | 多运行组数: {dd['multi_run_count']}")
        print(f"\n{'='*60}")
        generate_outputs(data)
        consistency_data = {exp: dd["trace_consistency_map"] for exp, dd in data.items()}
        print()

    if not args.skip_html:
        print("[2/2] 分组轨迹可视化...")
        for d in output_dirs:
            dirname = os.path.basename(d)
            consistency_map = consistency_data.get(dirname, {}) if consistency_data else {}
            generate_grouped_report(d, consistency_map=consistency_map)
        print()

    if not args.skip_index:
        print("[+] 生成总索引页...")
        generate_index(output_dirs, root_dir)
        print()

    print(f"{'='*60}")
    print(f"  分析完成!")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
