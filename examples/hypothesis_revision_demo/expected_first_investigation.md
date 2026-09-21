Investigation Summary

Confidence: 82%
Evidence Count: 4

Top Hypotheses:
1. Timeout value too small
2. HDR manager issue
3. Capture worker issue

Recommended Investigation:
- Inspect FrameSync.cpp
- Check the HDR capture wait path
- Verify capture sequencing around startCapture()
