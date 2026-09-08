#!/bin/bash
PYTHON="/Users/abrown/Documents/makerbotGcodeBridge/.venv/bin/python3"

"$PYTHON" "/Users/abrown/Documents/makerbotGcodeBridge/converter.py" --mesh-print "/Users/abrown/Documents/makerbotGcodeBridge/_pending_print.gcode" "/Users/abrown/Downloads/Fish base v1 T5_1.gcode"
EXIT_CODE=$?
echo ""
echo "--------------------------------------------"
if [ $EXIT_CODE -eq 0 ]; then
    echo "  All done."
else
    echo "  Exited with error code $EXIT_CODE."
fi
echo "--------------------------------------------"
read -rp "Press Enter to close..." _
