$ErrorActionPreference = "Stop"

$conda = "E:\ANACONDA\Scripts\conda.exe"
$python = "E:\ANACONDA\envs\nemotron_asr\python.exe"

if (!(Test-Path $conda)) {
    throw "Conda not found: $conda"
}

if (!(Test-Path $python)) {
    & $conda create -n nemotron_asr python=3.11 -y
}

& $python -m pip install --upgrade pip

# Nemotron's Transformers implementation requires torch >= 2.6.
& $python -m pip install torch==2.6.0+cu124 torchaudio==2.6.0+cu124 --index-url https://download.pytorch.org/whl/cu124

# NVIDIA's model card states Transformers support starts at 5.13.0.
& $python -m pip install "transformers>=5.13.0" accelerate numpy soundfile librosa huggingface_hub hf_transfer

Write-Host ""
Write-Host "Nemotron ASR environment is ready:"
Write-Host "  $python"
Write-Host ""
Write-Host "The first launch with ASR=nemotron will download:"
Write-Host "  nvidia/nemotron-3.5-asr-streaming-0.6b"
