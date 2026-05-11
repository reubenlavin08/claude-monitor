param([string]$text, [int]$rate = 4)
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
try { $s.SelectVoice("Microsoft David Desktop") } catch { }
$s.Rate = $rate
$s.Speak($text)
