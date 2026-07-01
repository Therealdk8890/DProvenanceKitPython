# DProvenanceKit demo GIF — render with:  vhs demo/demo.tape
# Produces assets/demo.gif

Output assets/demo.gif

Set Shell "bash"
Set FontSize 20
Set Width 1180
Set Height 640
Set Padding 24
Set Margin 14
Set BorderRadius 10
Set Theme "Catppuccin Mocha"
Set TypingSpeed 45ms
Set PlaybackSpeed 1.0

Hide
Type "cd $(git rev-parse --show-toplevel 2>/dev/null || pwd) && clear"
Enter
Show

Sleep 600ms
Type "python demo/demo_gif.py"
Sleep 400ms
Enter

# demo_gif.py paces its own output (~7s); wait past it, then hold on the final
# frame long enough to read the verdict. VHS does not wait for the command itself.
Sleep 11s
