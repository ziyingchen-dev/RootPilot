Investigation Summary

Confidence: 88%
Evidence Count: 5
Candidate Root Causes: 3

Top Hypotheses:

1. FrameSync timeout value too small
2. HDR capture path is not handling no-frame events
3. Capture worker exits before the frame arrives

Recommended Investigation:
Increase the FrameSync wait timeout and verify the HDR capture path waits long enough for the frame to arrive.
