package org.dced.player;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/** Handles the notification buttons (prev / play-pause / next / close) and "headphones unplugged". */
public class ControlReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context context, Intent intent) {
        String a = intent == null ? null : intent.getAction();
        if (a == null) return;
        if ("android.media.AUDIO_BECOMING_NOISY".equals(a)) {
            Bridge.push("pause");
            return;
        }
        int d = a.lastIndexOf('.');
        Bridge.push((d >= 0 ? a.substring(d + 1) : a).toLowerCase());
    }
}
