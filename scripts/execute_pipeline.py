import json
import subprocess
import os

def execute_plan(plan_path):
    print(f"--- Executing Automation Pipeline from: {plan_path} ---")

    if not os.path.exists(plan_path):
        print(f"Error: Execution plan not found at {plan_path}")
        return

    with open(plan_path, "r", encoding="utf-8") as f:
        plan = json.load(f)

    for task in plan:
        if task["type"] == "tool":
            print(f"\n[ACTION REQUIRED] 执行以下 TTS 生成命令:")
            print(f"  -> {task['call']}")
            # Manual trigger for safety in local environment

        elif task["type"] == "file_check":
            if os.path.exists(task["path"]):
                print(f"\n[CHECK OK] 自定义音频文件存在: {task['path']}")
            else:
                print(f"\n[ERROR] 找不到音频文件: {task['path']}")
                return

        elif task["type"] == "bash":
            print(f"\n[ACTION] 正在调用 FFmpeg 进行混音...")
            print(f"  -> 命令: {task['call']}")
            # For real usage, uncomment below:
            # subprocess.run(task['call'], shell=True, check=True)
            print("[INFO] FFmpeg 任务已准备好。如果需要自动运行，请确保 TTS 音频已就绪并取消 subprocess 的注释。")

if __name__ == "__main__":
    execute_plan("E:/MyClaudeProject/outputs/execution_plan.json")
