import json
import os
from pathlib import Path

def generate_execution_plan(plan_path):
    print(f"--- Generating Execution Plan: {plan_path} ---")

    if not os.path.exists(plan_path):
        print(f"Error: Generation plan not found at {plan_path}")
        return

    with open(plan_path, "r", encoding="utf-8") as f:
        plan = json.load(f)

    # Output list of tasks for the agent
    commands = []

    for item in plan:
        if item["type"] == "tts":
            # Generate the MCP tool call string
            cmd = f"audios_generation(texts=['{item['text']}'], voice_id='{item['voice_id']}', filenames=['{item['filename']}'])"
            commands.append({"type": "tool", "call": cmd})
        elif item["type"] == "original_audio":
            # Track file existence
            commands.append({"type": "file_check", "path": item["file_path"]})

    # Generate final ffmpeg command
    # Note: For production, this needs to be properly generated with timestamps
    ffmpeg_cmd = "ffmpeg -i tts_output.mp3 -i original_video.mp4 -filter_complex '[0:a][1:a]amix=inputs=2' final_video.mp4"
    commands.append({"type": "bash", "call": ffmpeg_cmd})

    # Save plan to a temporary file for the agent to read
    execution_plan_path = "E:/MyClaudeProject/outputs/execution_plan.json"
    with open(execution_plan_path, "w", encoding="utf-8") as f:
        json.dump(commands, f, ensure_ascii=False, indent=2)

    print(f"Execution plan saved to {execution_plan_path}")
    print("\n--- Execution Steps ---")
    for cmd in commands:
        print(cmd)

if __name__ == "__main__":
    generate_execution_plan("E:/MyClaudeProject/outputs/generation_plan.json")
