package org.dced.player;

import android.media.session.MediaSession;
import android.os.Bundle;

/**
 * Receives every transport command from the system media UI (notification shade,
 * lock screen, Android 13+ media carousel, Bluetooth / headset buttons).
 */
public class MediaCallback extends MediaSession.Callback {
    @Override public void onPlay() { Bridge.push("play"); }
    @Override public void onPause() { Bridge.push("pause"); }
    @Override public void onSkipToNext() { Bridge.push("next"); }
    @Override public void onSkipToPrevious() { Bridge.push("prev"); }
    @Override public void onStop() { Bridge.push("close"); }
    @Override public void onSeekTo(long pos) { Bridge.push("seek:" + pos); }
    @Override public void onCustomAction(String action, Bundle extras) { Bridge.push(action); }
}
