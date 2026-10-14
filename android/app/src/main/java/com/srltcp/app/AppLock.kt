package com.srltcp.app

import android.content.Context
import android.os.Build
import android.os.SystemClock
import android.view.Gravity
import android.view.View
import android.widget.Button
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.TextView
import androidx.biometric.BiometricManager
import androidx.biometric.BiometricManager.Authenticators.BIOMETRIC_WEAK
import androidx.biometric.BiometricManager.Authenticators.DEVICE_CREDENTIAL
import androidx.biometric.BiometricPrompt
import androidx.core.content.ContextCompat

/** This phone's privacy switches (Settings → Privacy). Both off by default. */
object PrivacyPrefs {
    private const val FILE = "srltcp_privacy"
    private const val APP_LOCK = "app_lock"
    private const val PRIVATE_NOTIFICATIONS = "private_notifications"

    private fun prefs(context: Context) =
        context.getSharedPreferences(FILE, Context.MODE_PRIVATE)

    fun appLock(context: Context): Boolean = prefs(context).getBoolean(APP_LOCK, false)

    fun setAppLock(context: Context, on: Boolean) =
        prefs(context).edit().putBoolean(APP_LOCK, on).apply()

    /** Lock screen shows "New message" without the sender or the text. */
    fun privateNotifications(context: Context): Boolean =
        prefs(context).getBoolean(PRIVATE_NOTIFICATIONS, false)

    fun setPrivateNotifications(context: Context, on: Boolean) =
        prefs(context).edit().putBoolean(PRIVATE_NOTIFICATIONS, on).apply()
}

/**
 * App lock: with it on, SRLTCP asks for the fingerprint, face or screen lock
 * when it's opened, and again after [LOCK_AFTER_MS] in the background. The
 * node keeps running while locked: messages and calls still arrive (a call
 * can be declined from its notification without unlocking).
 */
class AppLock(private val activity: MainActivity, private val root: FrameLayout) {
    val locked: Boolean get() = processLocked == true
    private var authenticating = false
    private var overlay: View? = null

    init {
        // Locked when the app process starts; an activity re-created on
        // rotation keeps the state it had.
        if (processLocked == null) processLocked = PrivacyPrefs.appLock(activity)
    }

    fun available(): Boolean =
        BiometricManager.from(activity).canAuthenticate(AUTHENTICATORS) ==
            BiometricManager.BIOMETRIC_SUCCESS

    fun onStart() {
        if (!PrivacyPrefs.appLock(activity)) {
            unlock()
            return
        }
        hideFromRecents(true)
        val away = backgroundAt > 0 && SystemClock.elapsedRealtime() - backgroundAt >= LOCK_AFTER_MS
        if (away) processLocked = true
        if (locked) {
            showOverlay()
            if (!authenticating) prompt("Unlock SRLTCP") { }
        }
    }

    fun onStop() {
        if (!authenticating) backgroundAt = SystemClock.elapsedRealtime()
    }

    /** Ask for the user's fingerprint/face/screen lock; [done] gets the result. */
    fun prompt(title: String, done: (Boolean) -> Unit) {
        if (authenticating) return
        authenticating = true
        val callback = object : BiometricPrompt.AuthenticationCallback() {
            override fun onAuthenticationSucceeded(result: BiometricPrompt.AuthenticationResult) {
                authenticating = false
                unlock()
                done(true)
            }

            override fun onAuthenticationError(errorCode: Int, errString: CharSequence) {
                authenticating = false
                done(false)  // stays locked; the Unlock button tries again
            }
        }
        val info = BiometricPrompt.PromptInfo.Builder()
            .setTitle(title)
            .setSubtitle("Fingerprint, face or your screen lock")
            .setAllowedAuthenticators(AUTHENTICATORS)
            .build()
        try {
            BiometricPrompt(activity, ContextCompat.getMainExecutor(activity), callback)
                .authenticate(info)
        } catch (e: Exception) {
            authenticating = false
            done(false)
        }
    }

    /** Turn the lock on (after the user proved they can unlock) or off. */
    fun setEnabled(on: Boolean, done: (String) -> Unit) {
        if (!on) {
            PrivacyPrefs.setAppLock(activity, false)
            hideFromRecents(false)
            unlock()
            done("off")
            return
        }
        if (!available()) {
            done("unavailable")
            return
        }
        prompt("Turn on app lock") { ok ->
            if (ok) {
                PrivacyPrefs.setAppLock(activity, true)
                hideFromRecents(true)
            }
            done(if (ok) "on" else "off")
        }
    }

    fun state(): String = when {
        PrivacyPrefs.appLock(activity) -> "on"
        available() -> "off"
        else -> "unavailable"
    }

    private fun unlock() {
        processLocked = false
        backgroundAt = 0L
        overlay?.let { root.removeView(it) }
        overlay = null
    }

    private fun hideFromRecents(hide: Boolean) {
        // The app switcher shows a blank card instead of the last screen.
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            activity.setRecentsScreenshotEnabled(!hide)
        }
    }

    private fun showOverlay() {
        if (overlay != null) return
        val panel = LinearLayout(activity).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER
            setBackgroundColor(0xFF0C0E14.toInt())
            isClickable = true  // nothing underneath can be touched
            isFocusable = true
            addView(TextView(activity).apply {
                text = "SRLTCP is locked"
                setTextColor(0xFFEEF1F8.toInt())
                textSize = 20f
                gravity = Gravity.CENTER
            })
            addView(TextView(activity).apply {
                text = "Messages and calls still arrive."
                setTextColor(0xFF8B93A8.toInt())
                textSize = 14f
                gravity = Gravity.CENTER
                setPadding(32, 16, 32, 48)
            })
            addView(Button(activity).apply {
                text = "Unlock"
                setOnClickListener { prompt("Unlock SRLTCP") { } }
            })
        }
        root.addView(
            panel,
            FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT
            )
        )
        overlay = panel
    }

    companion object {
        const val LOCK_AFTER_MS = 30_000L
        private var processLocked: Boolean? = null
        private var backgroundAt = 0L
        // Weak biometrics or the screen lock: works on every Android version
        // this app supports (strong-only plus credential needs Android 11).
        const val AUTHENTICATORS = BIOMETRIC_WEAK or DEVICE_CREDENTIAL
    }
}
