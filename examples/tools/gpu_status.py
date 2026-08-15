"""Example drop-in voice tool: GPU temperature, load, and VRAM use.

Copy into your VOICE_TOOLS_DIR. Needs an NVIDIA GPU with nvidia-smi on PATH.
"""
import subprocess

TOOL_DEF = {
    "type": "function",
    "name": "gpu_status",
    "description": (
        "Report the GPU's temperature, load, and memory use on this box. Use "
        "when the user asks how the GPU is doing, how hot it is, or how much "
        "VRAM is used."
    ),
    "parameters": {"type": "object", "properties": {}},
}
TIMEOUT_S = 4.0


def run(_arg=None) -> str:
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=temperature.gpu,utilization.gpu,memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=3,
        )
        line = result.stdout.strip().splitlines()[0]
        temp, util, used, total = (v.strip() for v in line.split(","))
        used_gb = int(used) / 1024
        total_gb = int(total) / 1024
        return (
            f"The GPU is at {temp} degrees, {util} percent load, "
            f"using {used_gb:.1f} of {total_gb:.1f} gigabytes."
        )
    except Exception:
        return "I couldn't read the GPU right now."
