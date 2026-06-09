import json
import os
from pathlib import Path

def parse_script(script_data):
    """
    Parses the script data and converts it into a generation plan
    compatible with audiobook skill.
    """
    plan = []

    for i, item in enumerate(script_data):
        if item["type"] == "tts":
            # Map to TTS generation entry
            entry = {
                "index": i,
                "type": "tts",
                "text": item["text"],
                "voice_id": item.get("voice_id", "narrator"),
                "filename": f"segment_{i}"
            }
            plan.append(entry)
        elif item["type"] == "original_audio":
            # Map to original audio reference entry
            entry = {
                "index": i,
                "type": "original_audio",
                "file_path": item["file_path"],
                "filename": f"segment_{i}"
            }
            plan.append(entry)

    return plan

if __name__ == "__main__":
    # Example usage with the user's provided script structure
    script_data = [
        {"type": "tts", "voice_id": "narrator", "text": "這個被暴雨困住的荒郊別墅裡，正坐著十三個各懷鬼胎的陌生人。"},
        {"type": "original_audio", "file_path": r"C:\Users\1\Desktop\undefined.mp3"},
        {"type": "tts", "voice_id": "narrator", "text": "突然，啪的一聲，別墅的燈光瞬間熄滅。"}
    ]

    plan = parse_script(script_data)

    # Save plan
    output_path = Path("E:/MyClaudeProject/outputs/generation_plan.json")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=2)

    print(f"Plan generated at {output_path}")
