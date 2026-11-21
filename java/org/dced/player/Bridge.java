package org.dced.player;

import java.util.concurrent.ConcurrentLinkedQueue;

/** Thread-safe mailbox: Android callbacks push commands, the Python service polls them. */
public class Bridge {
    private static final ConcurrentLinkedQueue<String> Q = new ConcurrentLinkedQueue<String>();

    public static void push(String s) {
        if (s != null) Q.add(s);
    }

    public static String poll() {
        return Q.poll();
    }
}
