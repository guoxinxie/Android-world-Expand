#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
clean_trajectories.py

对 parallel_runner_vllm.py 产出的 outputs-* 目录做轨迹清洗与补全：

1) 清理 none 动作：删除 model_output_action 为 None / none 的步骤，
   并把 images/ 与 xmls/ 下的图片、xml 按剩余步骤重新顺序命名（0,1,2...），
   同步修改 trace.json 中的 image_before / image_after 引用。

2) 补全 complete：对于同一 task_id 的任务组，如果组内有一条或多条轨迹
   输出了 status=complete，而某些轨迹没有输出 complete，但其最后一步的
   "动作 + 组件(xml)" 与某条已完成轨迹的倒数第二步（完成前的最后真实动作）
   一致，则判定该轨迹也已完成：在轨迹末尾追加一条 status=complete 步骤，
   其 image_before / image_after / xml 直接复制前一步的 after 图片与 xml。

用法:
    python clean_trajectories.py [ROOT_DIR] [--dry-run]
如不传 ROOT_DIR，默认使用当前目录。--dry-run 只打印不修改。
"""

import os
import re
import sys
import json
import shutil
import argparse


# ============================ 基础工具 ============================

def is_none_action(step):
    """判断某个 step 是否为 'none' / 空动作。"""
    a = step.get("model_output_action")
    if a is None:
        return True
    a = str(a).strip()
    if a == "" or a == "None" or a.lower() == "none":
        return True
    if "action_type='none'" in a or 'action_type="none"' in a:
        return True
    return False


def find_file(directory, stem):
    """在 directory 中按 stem 查找文件，自动尝试常见扩展名。"""
    if not os.path.isdir(directory):
        return None
    for ext in (".jpg", ".jpeg", ".png", ".xml"):
        p = os.path.join(directory, stem + ext)
        if os.path.exists(p):
            return p
    return None


def get_action_type(action_str):
    m = re.search(r"action_type='([^']+)'", str(action_str))
    return m.group(1) if m else "unknown"


def get_bracket(action_str):
    """提取动作字符串末尾的 [组件信息] 部分。"""
    m = re.search(r"\[(.*)\]\s*$", str(action_str))
    return m.group(1) if m else ""


def parse_control_info(action_str):
    m = re.search(r"\[(.*)\]\s*$", str(action_str))
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


def component_name_of(action_str):
    """得到被操作组件的“名字”（用于判定是否同一个组件）。"""
    ctrl = parse_control_info(str(action_str))
    name = (ctrl.get("content_desc") or ctrl.get("resource_id") or ctrl.get("text") or "").strip().lower()
    return name


def is_complete_step(step):
    a = str(step.get("model_output_action", ""))
    return "action_type='status'" in a and "goal_status='complete'" in a


def last_real_step(steps):
    """返回轨迹中最后一条真实（非 status）动作步骤；都非 status 时返回最后一条。"""
    for s in reversed(steps):
        if not is_complete_step(s):
            return s
    return steps[-1] if steps else None


# ============================ 1) 清理 none + 重命名 ============================

def renumber_run(run_path, dry_run=False):
    """清理 run 目录里的 none 步骤，并重命名图片/xml，返回 (移除步数, 总步数)。"""
    trace_file = os.path.join(run_path, "trace.json")
    if not os.path.exists(trace_file):
        return 0, 0

    with open(trace_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        data = [data]

    images_dir = os.path.join(run_path, "images")
    xmls_dir = os.path.join(run_path, "xmls")

    keep = [i for i, s in enumerate(data) if not is_none_action(s)]
    removed = len(data) - len(keep)

    if removed == 0:
        return 0, len(data)

    mapping = {old: new for new, old in enumerate(keep)}
    new_data = []
    rename_ops = []  # (src, dst)

    if not keep:
        # 整个轨迹都是 none -> 直接删除该空轨迹目录
        if not dry_run:
            shutil.rmtree(run_path, ignore_errors=True)
        return removed, 0

    for new, old in enumerate(keep):
        step = dict(data[old])
        before_src = find_file(images_dir, f"{old}_before")
        ext = os.path.splitext(before_src)[1] if before_src else ".jpg"

        new_before = f"{new}_before{ext}"
        new_after = f"{new}_after{ext}"
        step["image_before"] = new_before
        step["image_after"] = new_after
        new_data.append(step)

        if before_src:
            rename_ops.append((before_src, os.path.join(images_dir, new_before)))
        after_src = find_file(images_dir, f"{old}_after")
        if after_src:
            rename_ops.append((after_src, os.path.join(images_dir, new_after)))
        xml_src = find_file(xmls_dir, f"{old}_before")
        if xml_src:
            xml_ext = os.path.splitext(xml_src)[1]
            rename_ops.append((xml_src, os.path.join(xmls_dir, f"{new}_before{xml_ext}")))

    if dry_run:
        print(f"  [DRY] {os.path.basename(run_path)}: 移除 {removed} 个 none 步骤, "
              f"{len(data)} -> {len(new_data)}; 重命名 {len(rename_ops)} 个文件")
        return removed, len(new_data)

    # 两阶段重命名，避免同目录内文件名碰撞
    temps = []
    for src, dst in rename_ops:
        tmp = dst + ".tmprn"
        os.rename(src, tmp)
        temps.append((tmp, dst))
    for tmp, dst in temps:
        if os.path.exists(dst):
            os.remove(dst)
        os.rename(tmp, dst)

    with open(trace_file, "w", encoding="utf-8") as f:
        json.dump(new_data, f, ensure_ascii=False, indent=2)

    return removed, len(new_data)


# ============================ 2) 补全 complete ============================

def steps_match(incomplete_last, complete_final):
    """判断 incomplete 轨迹的最后一步，是否与 complete 轨迹完成前的最后真实动作一致。"""
    a1 = incomplete_last.get("action") or incomplete_last.get("model_output_action")
    a2 = complete_final.get("action") or complete_final.get("model_output_action")
    a1, a2 = str(a1), str(a2)
    t1, t2 = get_action_type(a1), get_action_type(a2)
    if t1 != t2:
        return False
    if t1 in ("status", "answer"):
        return a1 == a2

    b1, b2 = get_bracket(a1), get_bracket(a2)
    if b1 and b2:
        # 组件名（xml 中组件信息）一致即视为点击了同一个组件
        return b1 == b2

    # 退化为比较 index
    i1 = re.search(r"index=(\d+)", a1)
    i2 = re.search(r"index=(\d+)", a2)
    if i1 and i2:
        return i1.group(1) == i2.group(1)
    return False


def propagate_complete(exp_dir, dry_run=False):
    """在单个 outputs-* 实验目录内，对任务组补全 complete。返回补全的轨迹数。"""
    run_dirs = []
    for d in sorted(os.listdir(exp_dir)):
        dp = os.path.join(exp_dir, d)
        if os.path.isdir(dp) and os.path.exists(os.path.join(dp, "trace.json")):
            run_dirs.append(dp)

    # 按 task_id 分组
    groups = {}
    for dp in run_dirs:
        with open(os.path.join(dp, "trace.json"), "r", encoding="utf-8") as f:
            data = json.load(f)
        if not data:
            continue
        tid = str(data[0].get("task_id", ""))
        groups.setdefault(tid, []).append((dp, data))

    fixed = 0
    for tid, runs in groups.items():
        complete_final_steps = []
        for dp, data in runs:
            if is_complete_step(data[-1]):
                fr = last_real_step(data[:-1]) if len(data) > 1 else None
                if fr is not None:
                    complete_final_steps.append(fr)

        if not complete_final_steps:
            continue

        for dp, data in runs:
            if is_complete_step(data[-1]):
                continue
            last = data[-1]
            if not any(steps_match(last, cfs) for cfs in complete_final_steps):
                continue

            # 匹配成功 -> 追加 complete 步骤
            prev_idx = len(data) - 1
            new_idx = len(data)
            images_dir = os.path.join(dp, "images")
            xmls_dir = os.path.join(dp, "xmls")

            prev_after = find_file(images_dir, f"{prev_idx}_after")
            prev_xml = find_file(xmls_dir, f"{prev_idx}_before")
            ext = os.path.splitext(prev_after)[1] if prev_after else ".jpg"
            xml_ext = os.path.splitext(prev_xml)[1] if prev_xml else ".xml"

            new_before_img = f"{new_idx}_before{ext}"
            new_after_img = f"{new_idx}_after{ext}"
            new_xml = f"{new_idx}_before{xml_ext}"

            if dry_run:
                print(f"  [DRY] 补全 complete: {os.path.basename(dp)} (task={tid})")
                fixed += 1
                continue

            if prev_after:
                shutil.copy2(prev_after, os.path.join(images_dir, new_before_img))
                shutil.copy2(prev_after, os.path.join(images_dir, new_after_img))
            if prev_xml:
                shutil.copy2(prev_xml, os.path.join(xmls_dir, new_xml))

            data.append({
                "task_id": data[0].get("task_id"),
                "trace_id": data[0].get("trace_id"),
                "goal": data[0].get("goal"),
                "image_before": new_before_img,
                "image_after": new_after_img,
                "model_output_action": "JSONAction(action_type='status', goal_status='complete')",
                "action": "JSONAction(action_type='status', goal_status='complete')",
            })
            with open(os.path.join(dp, "trace.json"), "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            fixed += 1

    return fixed


# ============================ 入口 ============================

def find_exp_dirs(root):
    dirs = []
    for entry in sorted(os.listdir(root)):
        full = os.path.join(root, entry)
        if os.path.isdir(full) and entry.startswith("outputs-"):
            dirs.append(full)
    return dirs


def main():
    parser = argparse.ArgumentParser(description="清洗 none 动作 / 重命名 / 补全 complete")
    parser.add_argument("root", nargs="?", default=os.getcwd(), help="根目录（含 outputs-*）")
    parser.add_argument("--dry-run", action="store_true", help="只打印，不修改")
    args = parser.parse_args()

    root = os.path.abspath(args.root)
    exp_dirs = find_exp_dirs(root)
    if not exp_dirs:
        print(f"未在 {root} 中找到 outputs-* 目录")
        sys.exit(1)

    total_removed = 0
    total_renamed_runs = 0
    total_fixed = 0

    for exp in exp_dirs:
        print(f"\n=== 处理 {os.path.basename(exp)} ===")
        # 第一步：清理 + 重命名
        for d in sorted(os.listdir(exp)):
            dp = os.path.join(exp, d)
            if not os.path.isdir(dp):
                continue
            removed, total = renumber_run(dp, dry_run=args.dry_run)
            if removed > 0:
                total_removed += removed
                total_renamed_runs += 1
        # 第二步：补全 complete（依赖第一步结果）
        fixed = propagate_complete(exp, dry_run=args.dry_run)
        total_fixed += fixed
        print(f"  清理 none 步骤 {total_removed} 个（涉及 {total_renamed_runs} 条轨迹）；"
              f"补全 complete {fixed} 条轨迹")

    print(f"\n完成。总计: 移除 none {total_removed} 个, 补全 complete {total_fixed} 条。")


if __name__ == "__main__":
    main()
