bool startCapture()
{
    enable_hdr();
    return waitFrame();
}
