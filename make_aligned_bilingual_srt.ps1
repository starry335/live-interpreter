param(
    [Parameter(Mandatory)] [string] $Audio,
    [Parameter(Mandatory)] [string] $SourceSrt,
    [ValidateSet("zh", "ja", "en", "ko", "yue")] [string] $Language = "ja",
    [string] $Model = "large-v3",
    [ValidateRange(1, 16)] [int] $BatchSize = 4,
    [string] $OutputDir = (Join-Path $PSScriptRoot "outputs"),
    [ValidateSet("auto", "cuda", "cpu")] [string] $Device = "auto",
    [string] $ModelDir = (Join-Path $PSScriptRoot "models\whisperx")
)

$python = Join-Path $PSScriptRoot ".whisperenv\Scripts\python.exe"
foreach ($path in @($Audio, $SourceSrt, $python, (Join-Path $PSScriptRoot "align_bilingual_srt.py"))) {
    if (!(Test-Path -LiteralPath $path)) { throw "Not found: $path" }
}

$audioPath = (Resolve-Path -LiteralPath $Audio).Path
$srtPath = (Resolve-Path -LiteralPath $SourceSrt).Path
$outPath = if ([IO.Path]::IsPathRooted($OutputDir)) { $OutputDir } else { Join-Path $PSScriptRoot $OutputDir }
New-Item -ItemType Directory -Force -Path $outPath | Out-Null
$outPath = (Resolve-Path -LiteralPath $outPath).Path
New-Item -ItemType Directory -Force -Path $ModelDir | Out-Null
$modelPath = (Resolve-Path -LiteralPath $ModelDir).Path

if ($Device -eq "auto") {
    $Device = if ((& $python -c "import torch; print(torch.cuda.is_available())").Trim() -eq "True") { "cuda" } else { "cpu" }
}
$computeType = if ($Device -eq "cuda") { "float16" } else { "int8" }
$alignArgs = @()
if ($Language -eq "ja") {
    $alignModel = Join-Path $modelPath "align-ja-ivy"
    if (!(Test-Path -LiteralPath $alignModel)) { throw "Japanese alignment model is missing: $alignModel" }
    $alignArgs = @("--align_model", $alignModel)
}

& $python -m whisperx $audioPath --model $Model --model_dir $modelPath --language $Language --device $Device --compute_type $computeType --batch_size $BatchSize --output_dir $outPath @alignArgs
if ($LASTEXITCODE) { exit $LASTEXITCODE }

$transcript = Join-Path $outPath (([IO.Path]::GetFileNameWithoutExtension($audioPath)) + ".json")
if (!(Test-Path -LiteralPath $transcript)) { throw "WhisperX JSON was not created: $transcript" }

$stem = [IO.Path]::GetFileNameWithoutExtension($srtPath)
& $python (Join-Path $PSScriptRoot "align_bilingual_srt.py") $srtPath $transcript (Join-Path $outPath "$stem.aligned.json") (Join-Path $outPath "$stem.aligned.srt")
exit $LASTEXITCODE
