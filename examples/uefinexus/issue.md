## Issue

In the Memory Viewer, using **Goto** with an address outside the valid memory
ranges reports an error, but the viewer may no longer stay on the current page.
The status message says it is staying on the current page, so the cursor and
page should remain unchanged after an invalid address is entered.

## Steps to reproduce

1. Launch UefiNexus and open the Memory Viewer.
2. Note the current page address.
3. Press `g` and enter an address outside the valid memory ranges (for example,
   `0x9000` in the host integration test's mock memory map).
4. Observe the invalid-address message, then check whether the current page and
   cursor remain valid.

This is a RootPilot investigation test case based on the existing invalid-Goto
test and source-code path. The QEMU symptom has not been independently
reproduced.
