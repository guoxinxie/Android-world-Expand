import os
import json
import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns
import re
import platform
import numpy as np
from collections import defaultdict, Counter
from matplotlib.ticker import MaxNLocator

from task_matcher_utils import (
    get_task_info_by_goal,
    load_template_patterns,
    load_task_metadata,
    get_short_task_label,
    extract_timestamp_from_dir,
    cluster_traces_by_timestamp,
    group_traces_experiment,
)

if platform.system() == "Windows":
    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "sans-serif"]
else:
    plt.rcParams["font.sans-serif"] = ["DejaVu Sans", "SimHei", "sans-serif"]
plt.rcParams["axes.unicode_minus"] = False



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


def get_trace_info(trace_path):
    try:
        with open(trace_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not data:
            return None, None, 0, 0, False

        data_list = data if isinstance(data, list) else [data]
        first_step = data_list[0]
        goal = str(first_step.get("goal", "")).strip()

        raw_actions = [step.get("action", "") for step in data_list if "action" in step]
        parsed_actions = [parse_action_signature(act) for act in raw_actions]

        unknown_count = sum(1 for act in parsed_actions if act == "unknown")
        valid_actions = [act for act in parsed_actions if act != "unknown"]

        last_raw_action = str(raw_actions[-1]) if raw_actions else ""
        is_completed = "action_type='status'" in last_raw_action and "goal_status='complete'" in last_raw_action

        return goal, tuple(valid_actions), len(raw_actions), unknown_count, is_completed
    except Exception as e:
        print(f"读取 {trace_path} 出错: {e}")
        return None, None, 0, 0, False



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
    metrics = {"n": n, "mean_distance": round(mn, 4), "max_distance": round(mx, 4), "std_distance": round((sum((d - mn)**2 for d in dists) / len(dists))**0.5, 4), "majority_ratio": round(mr, 4)}
    
    
    st, nt, mt = thresholds
    if mx == 0.0:
        return 0, metrics

    # n-1 majority rule: if n-1 traces identical and outlier within mt, classify as similar
    if n >= 3:
        sc2 = Counter(sequences)
        mc2_seq, mc2_count = sc2.most_common(1)[0]
        if mc2_count >= n - 1:
            outlier_dist = 0.0
            for seq2 in sequences:
                if seq2 != mc2_seq:
                    d2 = normalized_levenshtein(seq2, mc2_seq)
                    if d2 > outlier_dist:
                        outlier_dist = d2
            if outlier_dist <= thresholds[2]:
                return 1, metrics
    
    if mx <= nt:
        return 1, metrics
    if mx <= mt:
        return 2, metrics
    return 3, metrics


def analyze_trajectories(root_dir, templates_map=None, difficulty_map=None):
   
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

            goal, actions, raw_steps, unk_count, is_comp = get_trace_info(trace_file)
            if goal and actions is not None:
                total_unknown_actions += unk_count
                _, task_name = get_task_info_by_goal(goal, templates_map, difficulty_map)
                all_traces.append({
                    "id": run_dir,
                    "goal": goal,
                    "task_name": task_name,
                    "actions": actions,
                    "raw_steps": raw_steps,
                    "is_completed": is_comp,
                })


        clusters = cluster_traces_by_timestamp(all_traces, time_threshold_seconds=180)

        multi_run_clusters = [c for c in clusters if len(c) > 1]

        strict_consistent = []
        similar = []
        moderate = []
        divergent = []
        task_all_completed = 0

        for cluster in multi_run_clusters:
            task_name = cluster[0]["task_name"]
            seqs = [t["actions"] for t in cluster]
            ctype, cmetrics = get_consistency_type_v2(seqs)

            is_all_completed = all(t["is_completed"] for t in cluster)
            if is_all_completed:
                task_all_completed += 1

            sample_goal = cluster[0]["goal"]
            seq_counter = Counter(seqs)
            representative_seq = seq_counter.most_common(1)[0][0]
            step_counts = [t["raw_steps"] for t in cluster]

            info = (task_name, len(cluster), sample_goal, representative_seq,
                    [t["id"] for t in cluster], is_all_completed, step_counts)

            if ctype == 0:
                strict_consistent.append(info)
            elif ctype == 1:
                similar.append(info)
            elif ctype == 2:
                moderate.append(info)
            elif ctype >= 3:
                divergent.append(info)

        # Build trace_id to consistency mapping for HTML visualization
        trace_consistency_map = {}
        for _ctype, _clusters in [(0, strict_consistent), (1, similar), (2, moderate), (3, divergent)]:
            _clabel = {0: "\u5b8c\u5168\u4e00\u81f4", 1: "\u9ad8\u5ea6\u76f8\u4f3c", 2: "\u4e2d\u7b49\u76f8\u4f3c", 3: "\u4e25\u91cd\u5206\u6b67"}[_ctype]
            for _info in _clusters:
                for _tid in _info[4]:
                    trace_consistency_map[_tid] = {"type": _ctype, "label": _clabel}
        report_data[exp_dir] = {
            "total_unique_tasks": len(set(t["task_name"] for t in all_traces)),
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
        fu.write("============= 动作统报告 =============\n\n")
        for exp_dir, data in report_data.items():
            fu.write(f"{exp_dir:<35} -> 动作总数: {data['total_unknown_actions']}\n")

    with open("trajectory_analysis_report.txt", "w", encoding="utf-8") as f:
        f.write("轨迹致分析报告\n")
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
            f.write(f"  实验组数 (时间戳聚类): {data['multi_run_count']}\n")
            f.write(f"  全部运行完成的实验组数: {data['task_all_completed']}\n")
            f.write(f"  未知动作总数: {data['total_unknown_actions']}\n")
            f.write(f"{'-' * 70}\n")

            strict_comp = sum(1 for info in data["strict"] if info[5])
            sim_comp = sum(1 for info in data["similar"] if info[5])
            mod_comp = sum(1 for info in data["moderate"] if info[5])
            div_comp = sum(1 for info in data["divergent"] if info[5])

            f.write(f"\n--- 完全一致({len(data['strict'])} ? 全部完成 {strict_comp} ? ---\n")
            for task, r_count, goal, seq, dirs, all_comp, step_counts in data["strict"]:
                short_label = get_short_task_label(task)
                f.write(f"  [{task}] ({r_count} 次运行 {'[全部完成]' if all_comp else '[部分完成]'}\n")
                f.write(f"    标签: {short_label}\n")
                f.write(f"    目标: {goal}\n")
                f.write(f"    动作序列: {' -> '.join(seq)}\n")
                f.write(f"    各轨迹步数 {step_counts}\n")

            f.write(f"\n--- 高度相似 ({len(data['similar'])} ? 全部完成 {sim_comp} ? ---\n")
            for task, r_count, goal, seq, dirs, all_comp, step_counts in data["similar"]:
                short_label = get_short_task_label(task)
                f.write(f"  [{task}] ({r_count} 次运行 {'[全部完成]' if all_comp else '[部分完成]'}\n")
                f.write(f"    标签: {short_label}\n")
                f.write(f"    目标: {goal}\n")
                f.write(f"    代表动作序列: {' -> '.join(seq)}\n")
                f.write(f"    各轨迹步数 {step_counts}\n")
                f.write(f"    轨迹目录:\n")
                for i, d in enumerate(dirs):
                    f.write(f"      [{i+1}] ...{d[-30:]}\n")


            f.write(f"\n--- 中等相似 ({len(data['moderate'])} ? 全部完成 {mod_comp} ? ---\n")
            for task, r_count, goal, seq, dirs, all_comp, step_counts in data["moderate"]:
                short_label = get_short_task_label(task)
                f.write(f"  [{task}] ({r_count} 次运行 {chr(39) + chr(91) + chr(20840) + chr(37096) + chr(23436) + chr(25104) + chr(93) + chr(39) if all_comp else chr(39) + chr(91) + chr(37096) + chr(20998) + chr(23436) + chr(25104) + chr(93) + chr(39)}\n")
                f.write(f"    标签: {short_label}\n")
                f.write(f"    目标: {goal}\n")
                f.write(f"    各轨迹步数 {step_counts}\n")
                f.write(f"    轨迹目录:\n")
                for i, d in enumerate(dirs):
                    f.write(f"      [{i+1}] ...{d[-30:]}\n")

            f.write(f"\n--- 严重分歧 ({len(data['divergent'])} ? 全部完成 {div_comp} ? ---\n")
            for task, r_count, goal, seq, dirs, all_comp, step_counts in data["divergent"]:
                short_label = get_short_task_label(task)
                f.write(f"  [{task}] ({r_count} 次运行 {'[全部完成]' if all_comp else '[部分完成]'}\n")
                f.write(f"    标签: {short_label}\n")
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
                                 xy=(rect.get_x() + rect.get_width() / 2, h / 2),
                                 xytext=(0, 0), textcoords="offset points",
                                 ha="center", va="center", fontweight="bold", fontsize=11,
                                 color="white" if h > 1.5 else "#333")

    autolabel([rects1, rects2_bottom, rects2_top, rects4])


    for i in range(len(df)):
        total_groups = df.iloc[i]["total_participating"]
        if total_groups <= 0:
            total_groups = df.iloc[i]["strict_total"] + df.iloc[i]["sm_total"] + df.iloc[i]["div_total"]

        # Compute all percentages and adjust to ensure sum = 100%
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
    print("图表和报告已成功生成")
    print("  -> trajectory_consistency_chart.png")
    print("  -> trajectory_analysis_report.txt")
    print("  -> unknown_actions_report.txt")


    # 4. Output consistency JSON for visualize_trace.py
    consistency_json = {}
    for exp_dir, data in report_data.items():
        consistency_json[exp_dir] = data.get("trace_consistency_map", {})
    json_path = "trajectory_consistency.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(consistency_json, f, ensure_ascii=False, indent=2)
    print(f"  -> {json_path}")


if __name__ == "__main__":
    current_dir = os.getcwd()
    print("正在分析轨迹一致性...\n")

    templates_map = load_template_patterns()
    difficulty_map = load_task_metadata()
    print(f"已加载{len(templates_map)}个任务模板 {len(difficulty_map)}个难度映射\n")

    data = analyze_trajectories(current_dir, templates_map, difficulty_map)

    for exp_dir, d in data.items():
        print(f"{'='*60}")
        print(f"实验组 {exp_dir}")
        print(f"  完全一致: {len(d['strict'])} | 高度相似: {len(d['similar'])} | 中等相似: {len(d["moderate"])} | 严重分歧: {len(d['divergent'])}")
        print(f"  任务模板类型: {d['total_unique_tasks']} | 实验组数: {d['multi_run_count']}")

    print(f"\n{'='*60}")
    generate_outputs(data)
