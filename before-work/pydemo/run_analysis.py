#!/usr/bin/env python3
"""
一键分析入口
============================
用法:
  python run_analysis.py                           # 分析当前目录下所有 outputs-* 目录
  python run_analysis.py --dir /path/to/data       # 分析指定目录
  python run_analysis.py --dirs dir1 dir2          # 分析多个指定目录

"""

import os
import sys
import argparse
import subprocess
import glob
import json
from datetime import datetime


def find_output_dirs(root_dir):
    dirs = []
    for entry in os.listdir(root_dir):
        full = os.path.join(root_dir, entry)
        if os.path.isdir(full) and entry.startswith("outputs-"):
            dirs.append(full)
    return sorted(dirs)


def check_dependencies():
    missing = []
    required_files = ["task_matcher_utils.py", "analyze_consistency.py", "visualize_trace.py"]
    for f in required_files:
        if not os.path.exists(f):
            missing.append(f)
    return missing


def run_analysis(script_name, root_dir, timeout=300):
    print(f"  -> 运行 {script_name} ...")
    result = subprocess.run(
        [sys.executable, script_name],
        cwd=root_dir,
        capture_output=True,
        text=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode == 0:
        print(f"     [OK]")
        return True, result.stdout
    else:
        print(f"     [失败] {result.stderr[:200]}")
        return False, result.stderr


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
    html += f'<p>工具文件: task_matcher_utils.py, analyze_consistency.py, visualize_trace.py</p>'
    html += f'<p>数据目录数: {len(output_dirs)}</p>'
    html += f'</div>\n'
    html += "</body></html>"

    index_path = os.path.join(output_dir, "index.html")
    with open(index_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"总索引页已生成: {index_path}")
    return index_path


def main():
    parser = argparse.ArgumentParser(
        description=" 运行分析",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
              开始分析
        """,
    )
    parser.add_argument("--dir", default=None, help="分析根目录（默认为当前目录）")
    parser.add_argument("--dirs", nargs="+", default=None, help="分析多个指定目录")
    parser.add_argument("--skip-chart", action="store_true", help="跳过一致性分析")
    parser.add_argument("--skip-html", action="store_true", help="跳过 HTML 生成")
    parser.add_argument("--skip-index", action="store_true", help="跳过索引页生成")
    args = parser.parse_args()

    # 确定根目录
    if args.dirs:
        output_dirs = args.dirs
        root_dir = os.path.commonpath(output_dirs) if len(output_dirs) > 1 else os.path.dirname(output_dirs[0])
    elif args.dir:
        root_dir = args.dir
    else:
        root_dir = os.getcwd()

    root_dir = os.path.abspath(root_dir)
    print(f"{'='*60}")
    print(f"工作目录: {root_dir}")
    print()

   
    missing = check_dependencies()
    if missing:
        print(f"[错误] 缺少必需文件: {', '.join(missing)}")
        print(f"请确保以下文件存在于当前目录:")
        print(f"  - task_matcher_utils.py")
        print(f"  - analyze_consistency.py")
        print(f"  - visualize_trace.py")
        sys.exit(1)

    
    if not args.dirs:
        output_dirs = find_output_dirs(root_dir)
    if not output_dirs:
        print(f"[警告] 未找到 outputs-* 目录")
        print(f"请确保数据目录以 'outputs-' 开头")
        sys.exit(1)

    print(f"发现 {len(output_dirs)} 个数据目录:")
    for d in output_dirs:
        print(f"  - {os.path.basename(d)}")
    print()

    
    if not args.skip_chart:
        print("[1/2] 轨迹一致性分析...")
        success, _ = run_analysis("analyze_consistency.py", root_dir)
        if not success:
            print("  [跳过] 一致性分析失败，继续运行其他步骤")
        print()

    
    if not args.skip_html:
        print("[2/2] 分组轨迹可视化...")
        success, _ = run_analysis("visualize_trace.py", root_dir)
        if not success:
            print("  [跳过] HTML 生成失败")
        print()

    
    if not args.skip_index:
        print("[+] 生成总索引页...")
        generate_index(output_dirs, root_dir)
        print()

   
    print(f"{'='*60}")
    print(f"  分析完成!")
    print(f"{'='*60}")
    print()
    print(f"生成的文件:")
    chart = os.path.join(root_dir, "trajectory_consistency_chart.png")
    if os.path.exists(chart):
        print(f"  [chart] {chart}")
    report = os.path.join(root_dir, "trajectory_analysis_report.txt")
    if os.path.exists(report):
        print(f"  [report] {report}")
    for d in output_dirs:
        html = os.path.join(d, "grouped_report.html")
        if os.path.exists(html):
            print(f"  [html] {html}")
    index = os.path.join(root_dir, "index.html")
    if os.path.exists(index):
        print(f"  [index] {index}")



if __name__ == "__main__":
    main()
