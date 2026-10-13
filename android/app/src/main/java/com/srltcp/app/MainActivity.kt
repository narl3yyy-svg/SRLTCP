package com.srltcp.app

import android.Manifest
import android.annotation.SuppressLint
import android.nfc.NfcAdapter
import android.nfc.Tag
import android.webkit.PermissionRequest
import org.json.JSONObject
import com.journeyapps.barcodescanner.ScanContract
import com.journeyapps.barcodescanner.ScanOptions
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.net.http.SslCertificate
import android.net.http.SslError
import android.os.Build
import android.os.Bundle
import android.os.Environment
import android.os.Handler
import android.os.Looper
import android.provider.DocumentsContract
import android.provider.Settings
import android.util.Log
import android.view.Gravity
import android.webkit.SslErrorHandler
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.widget.FrameLayout
import android.widget.TextView
import androidx.activity.OnBackPressedCallback
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.webkit.WebViewClientCompat
import com.chaquo.python.Python
import java.security.MessageDigest

class MainActivity : AppCompatActivity() {
    private var webView: WebView? = null
    private var statusView: TextView? = null
    private val handler = Handler(Looper.getMainLooper())
    private var loadAttempt = 0
    private var pageLoaded = false
    private var uiRestored = false
    private var firstLaunch = false
    private var filePathCallback: ValueCallback<Array<Uri>>? = null
    private var isClosing = false

    var isActivityResumed = false
        private set

    /** True while the main frame is our own https://127.0.0.1 UI (checked by the JS bridge). */
    @Volatile
    var isTrustedPage = false
        private set

    private val fileChooserLauncher = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) { result ->
        val callback = filePathCallback
        filePathCallback = null
        if (callback == null) return@registerForActivityResult
        // parseResult() only returns one file; with "select multiple" the
        // picker returns them in clipData.
        val clip = result.data?.clipData
        val uris = if (result.resultCode == RESULT_OK && clip != null && clip.itemCount > 0) {
            Array(clip.itemCount) { clip.getItemAt(it).uri }
        } else {
            WebChromeClient.FileChooserParams.parseResult(result.resultCode, result.data)
        }
        callback.onReceiveValue(uris)
    }

    private val qrScanLauncher = registerForActivityResult(ScanContract()) { result ->
        val text = result.contents ?: return@registerForActivityResult
        deliverToWeb("onAndroidQrResult", JSONObject.quote(text))
    }

    /** File waiting for the user to pick a save location (Save as… for received files). */
    private var pendingSavePath: String? = null

    private val saveFileLauncher = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) { result ->
        val source = pendingSavePath
        pendingSavePath = null
        val target = result.data?.data
        if (source == null && result.resultCode == RESULT_OK && target != null) {
            // Activity was recreated and the path lost: drop the empty document.
            Thread {
                try {
                    DocumentsContract.deleteDocument(contentResolver, target)
                } catch (e: Exception) {
                    Log.w(TAG, "Could not delete empty document", e)
                }
                deliverToWeb("onAndroidFileSaved", "false")
            }.start()
            return@registerForActivityResult
        }
        if (source == null || result.resultCode != RESULT_OK || target == null) return@registerForActivityResult
        Thread {
            val ok = try {
                contentResolver.openOutputStream(target)?.use { out ->
                    java.io.File(source).inputStream().use { it.copyTo(out) }
                    true
                } ?: false
            } catch (e: Exception) {
                Log.e(TAG, "Save file failed", e)
                false
            }
            deliverToWeb("onAndroidFileSaved", if (ok) "true" else "false")
        }.start()
    }

    private var pendingWebPermission: PermissionRequest? = null

    private val requestMediaPermissions = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { grants ->
        val request = pendingWebPermission ?: return@registerForActivityResult
        pendingWebPermission = null
        val allowed = request.resources.filter { res ->
            when (res) {
                PermissionRequest.RESOURCE_AUDIO_CAPTURE -> grants[Manifest.permission.RECORD_AUDIO] == true ||
                    hasPermission(Manifest.permission.RECORD_AUDIO)
                PermissionRequest.RESOURCE_VIDEO_CAPTURE -> grants[Manifest.permission.CAMERA] == true ||
                    hasPermission(Manifest.permission.CAMERA)
                else -> false
            }
        }
        if (allowed.isEmpty()) request.deny() else request.grant(allowed.toTypedArray())
    }

    private val requestNotificationPermission = registerForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { _ ->
        maybeStartForegroundService()
        // Asked only now so the two system prompts do not stack.
        requestStorageAccess()
    }

    private val requestStoragePermissions = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { _ -> }

    private val requestAllFilesAccess = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) { _ -> }

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        current = java.lang.ref.WeakReference(this)
        SRLTCPNotifier.ensureChannels(this)
        firstLaunch = savedInstanceState == null
        pendingSavePath = savedInstanceState?.getString(STATE_PENDING_SAVE)

        SRLTCPApplication.pythonError?.let {
            showFatal("Python failed to start:\n$it")
            return
        }

        if (!Python.isStarted()) {
            showFatal("Python runtime not initialized.\nRestart the app.")
            return
        }

        val root = FrameLayout(this)
        statusView = TextView(this).apply {
            text = "Starting SRLTCP…"
            setTextColor(0xFF8B93A8.toInt())
            gravity = Gravity.CENTER
            textSize = 14f
            setPadding(48, 48, 48, 48)
        }
        root.addView(statusView)

        webView = WebView(this).also { wv ->
            wv.visibility = android.view.View.GONE
            wv.settings.javaScriptEnabled = true
            wv.settings.domStorageEnabled = true
            // The UI is served over https://127.0.0.1; never load file:// URLs.
            wv.settings.allowFileAccess = false
            wv.settings.allowContentAccess = true  // file picker returns content:// URIs
            wv.settings.databaseEnabled = true
            wv.settings.useWideViewPort = true
            wv.settings.loadWithOverviewMode = true
            wv.settings.mixedContentMode = android.webkit.WebSettings.MIXED_CONTENT_NEVER_ALLOW
            // Remote call audio/video starts from code, not from a tap on the
            // <video>; without this the WebView shows a play button and stays silent.
            wv.settings.mediaPlaybackRequiresUserGesture = false
            // The app scales to the screen; only the image viewer zooms (in JS).
            wv.settings.setSupportZoom(false)
            wv.settings.builtInZoomControls = false
            wv.settings.displayZoomControls = false
            WebView.setWebContentsDebuggingEnabled(BuildConfig.DEBUG)
            wv.addJavascriptInterface(SRLTCPBridge(this@MainActivity), "SRLTCPAndroid")
            wv.webChromeClient = object : WebChromeClient() {
                override fun onPermissionRequest(request: PermissionRequest) {
                    // Only our own local UI may use the camera/microphone (QR + calls).
                    if (request.origin.host != "127.0.0.1") {
                        request.deny()
                        return
                    }
                    runOnUiThread {
                        val needed = mutableListOf<String>()
                        if (PermissionRequest.RESOURCE_AUDIO_CAPTURE in request.resources &&
                            !hasPermission(Manifest.permission.RECORD_AUDIO)
                        ) needed.add(Manifest.permission.RECORD_AUDIO)
                        if (PermissionRequest.RESOURCE_VIDEO_CAPTURE in request.resources &&
                            !hasPermission(Manifest.permission.CAMERA)
                        ) needed.add(Manifest.permission.CAMERA)
                        if (needed.isEmpty()) {
                            request.grant(request.resources.filter {
                                it == PermissionRequest.RESOURCE_AUDIO_CAPTURE ||
                                    it == PermissionRequest.RESOURCE_VIDEO_CAPTURE
                            }.toTypedArray())
                        } else {
                            pendingWebPermission?.deny()
                            pendingWebPermission = request
                            requestMediaPermissions.launch(needed.toTypedArray())
                        }
                    }
                }

                override fun onShowFileChooser(
                    webView: WebView,
                    filePathCallback: ValueCallback<Array<Uri>>,
                    fileChooserParams: FileChooserParams
                ): Boolean {
                    this@MainActivity.filePathCallback?.onReceiveValue(null)
                    this@MainActivity.filePathCallback = filePathCallback
                    return try {
                        fileChooserLauncher.launch(fileChooserParams.createIntent())
                        true
                    } catch (e: Exception) {
                        Log.e(TAG, "File chooser failed", e)
                        this@MainActivity.filePathCallback = null
                        false
                    }
                }
            }
            wv.webViewClient = object : WebViewClientCompat() {
                override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                    val uri = request.url
                    if (uri.host == "127.0.0.1" && uri.scheme == "https") return false
                    // Never load outside pages inside the privileged app WebView.
                    if (uri.scheme == "http" || uri.scheme == "https") {
                        try {
                            startActivity(Intent(Intent.ACTION_VIEW, uri))
                        } catch (e: Exception) {
                            Log.w(TAG, "No app to open $uri", e)
                        }
                    }
                    return true
                }

                override fun onPageStarted(view: WebView, url: String?, favicon: android.graphics.Bitmap?) {
                    isTrustedPage = isOurUrl(url)
                    super.onPageStarted(view, url, favicon)
                }

                override fun onPageFinished(view: WebView, url: String) {
                    isTrustedPage = isOurUrl(url)
                    if (isClosing || isFinishing || url == "about:blank") return
                    pageLoaded = true
                    loadAttempt = 0
                    handler.removeCallbacks(loadRetryRunnable)
                    view.evaluateJavascript(
                        "document.documentElement.classList.add('android-app');" +
                            "document.getElementById('stat-cpu')?.classList.add('hidden');" +
                            "if(window.applyMobileLayout)window.applyMobileLayout('android');",
                        null
                    )
                    statusView?.visibility = android.view.View.GONE
                    view.visibility = android.view.View.VISIBLE
                    maybeStartForegroundService()
                }

                override fun onReceivedSslError(view: WebView, handler: SslErrorHandler, error: SslError) {
                    if (isClosing || isFinishing) {
                        handler.cancel()
                        return
                    }
                    val host = android.net.Uri.parse(error.url).host
                    if ((host == "127.0.0.1" || host == "localhost") && isOurCertificate(error)) {
                        handler.proceed()
                    } else {
                        Log.w(TAG, "Rejected TLS certificate for ${error.url}")
                        handler.cancel()
                    }
                }

                @Suppress("DEPRECATION", "OVERRIDE_DEPRECATION")
                override fun onReceivedError(
                    view: WebView,
                    errorCode: Int,
                    description: String?,
                    failingUrl: String?
                ) {
                    if (isClosing || isFinishing) {
                        view.stopLoading()
                        return
                    }
                    super.onReceivedError(view, errorCode, description, failingUrl)
                }
            }
            root.addView(wv)
        }
        setContentView(root)
        NfcPairing.listener = { result ->
            if (!result.contains("\"error\"")) {
                vibrate(longArrayOf(0, 60, 80, 60))
                runOnUiThread { stopNfcReader() }
            }
            deliverToWeb("onAndroidNfcResult", JSONObject.quote(result))
        }

        if (savedInstanceState != null) {
            webView?.restoreState(savedInstanceState)
            val url = webView?.url
            if (!url.isNullOrBlank() && url != "about:blank") {
                uiRestored = true
                pageLoaded = true
                webView?.visibility = android.view.View.VISIBLE
                statusView?.visibility = android.view.View.GONE
            }
        }

        onBackPressedDispatcher.addCallback(
            this,
            object : OnBackPressedCallback(true) {
                override fun handleOnBackPressed() {
                    val wv = webView ?: run {
                        moveTaskToBack(true)
                        return
                    }
                    wv.evaluateJavascript(
                        "(function(){return window.androidNavigateBack" +
                            "?!!window.androidNavigateBack():false;})()"
                    ) { result ->
                        if (result != "true") {
                            // On the main screen: go to the background like other
                            // messengers instead of closing the app (the node keeps
                            // running and reopening is instant).
                            moveTaskToBack(true)
                        }
                    }
                }
            }
        )

        beginServerStartup()
        if (!requestNotificationIfNeeded()) requestStorageAccess()
    }

    fun postAlertNotification(title: String, body: String, tag: String) {
        if (isClosing || isFinishing) return
        if (isActivityResumed) return
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            ContextCompat.checkSelfPermission(
                this,
                Manifest.permission.POST_NOTIFICATIONS
            ) != PackageManager.PERMISSION_GRANTED
        ) {
            return
        }
        SRLTCPNotifier.postAlert(this, title, body, tag)
    }

    /** Optional storage access; asked on a fresh launch only, and the settings screen just once. */
    private fun requestStorageAccess() {
        if (!firstLaunch || isClosing) return
        firstLaunch = false

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            val prefs = getSharedPreferences(PREFS, MODE_PRIVATE)
            if (!Environment.isExternalStorageManager() && !prefs.getBoolean(PREF_ALL_FILES_ASKED, false)) {
                prefs.edit().putBoolean(PREF_ALL_FILES_ASKED, true).apply()
                try {
                    requestAllFilesAccess.launch(
                        Intent(Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION).apply {
                            data = Uri.parse("package:$packageName")
                        }
                    )
                } catch (e: Exception) {
                    Log.w(TAG, "All files access screen unavailable", e)
                }
            }
            return
        }

        val needed = mutableListOf<String>()
        if (ContextCompat.checkSelfPermission(
                this,
                Manifest.permission.READ_EXTERNAL_STORAGE
            ) != PackageManager.PERMISSION_GRANTED
        ) {
            needed.add(Manifest.permission.READ_EXTERNAL_STORAGE)
        }
        if (ContextCompat.checkSelfPermission(
                this,
                Manifest.permission.WRITE_EXTERNAL_STORAGE
            ) != PackageManager.PERMISSION_GRANTED
        ) {
            needed.add(Manifest.permission.WRITE_EXTERNAL_STORAGE)
        }
        if (needed.isNotEmpty()) {
            requestStoragePermissions.launch(needed.toTypedArray())
        }
    }

    private fun beginServerStartup() {
        startPythonServer()
        if (uiRestored && isServerReady()) {
            maybeStartForegroundService()
            return
        }
        waitForServerThenLoad()
    }

    private fun startPythonServer() {
        val app = SRLTCPApplication.instance ?: return
        app.ensurePythonServer { msg -> handler.post { showFatal("Server start failed:\n$msg") } }
    }

    private fun waitForServerThenLoad() {
        Thread {
            try {
                val py = Python.getInstance()
                var waited = 0
                while (waited < 90000) {
                    val ready = py.getModule("srltcp.app")
                        .callAttr("is_android_server_ready")
                        .toBoolean()
                    if (ready) {
                        val port = py.getModule("srltcp.app")
                            .callAttr("get_android_web_port")
                            .toInt()
                        handler.post {
                            statusView?.text = "Loading UI on port $port…"
                            scheduleLoad(400)
                        }
                        return@Thread
                    }
                    Thread.sleep(250)
                    waited += 250
                }
                handler.post {
                    showFatal(
                        "SRLTCP server did not start within 90 seconds.\n" +
                            "Check logcat for SRLTCP tag."
                    )
                }
            } catch (e: Exception) {
                Log.e(TAG, "Server wait failed", e)
                handler.post { showFatal("Failed to reach server:\n${e.message}") }
            }
        }.apply { name = "srltcp-wait"; start() }
    }

    /** Returns true if the prompt was shown (its callback continues startup). */
    private fun requestNotificationIfNeeded(): Boolean {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) return false
        if (ContextCompat.checkSelfPermission(
                this,
                Manifest.permission.POST_NOTIFICATIONS
            ) != PackageManager.PERMISSION_GRANTED
        ) {
            requestNotificationPermission.launch(Manifest.permission.POST_NOTIFICATIONS)
            return true
        }
        return false
    }

    private fun maybeStartForegroundService() {
        // The service clears this if startForeground failed, so a later call retries.
        if (SRLTCPService.running) return
        try {
            val serviceIntent = Intent(this, SRLTCPService::class.java)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                ContextCompat.startForegroundService(this, serviceIntent)
            } else {
                startService(serviceIntent)
            }
        } catch (e: Exception) {
            Log.w(TAG, "Foreground service not started (UI still works)", e)
        }
    }

    private val loadUiRunnable = Runnable { loadWebUi() }
    private val loadRetryRunnable = Runnable {
        if (isClosing || pageLoaded) return@Runnable
        val wv = webView ?: return@Runnable
        if (wv.visibility == android.view.View.VISIBLE) return@Runnable
        Log.w(TAG, "Web UI not ready yet, retrying…")
        scheduleLoad(2000)
    }

    private fun scheduleLoad(delayMs: Long) {
        if (isClosing) return
        handler.removeCallbacks(loadUiRunnable)
        handler.removeCallbacks(loadRetryRunnable)
        handler.postDelayed(loadUiRunnable, delayMs)
    }

    private fun loadWebUi() {
        if (isClosing) return
        val wv = webView ?: return
        if (!isServerReady()) {
            waitForServerThenLoad()
            return
        }
        loadAttempt++
        if (loadAttempt > 12) {
            showFatal("Cannot reach SRLTCP web UI on port ${resolvePort()}.")
            return
        }
        pageLoaded = false
        handler.removeCallbacks(loadRetryRunnable)
        val port = resolvePort()
        val base = "https://127.0.0.1:$port/"
        val token = try {
            Python.getInstance().getModule("srltcp.app").callAttr("get_android_web_token").toString()
        } catch (e: Exception) {
            Log.e(TAG, "Could not read web token", e)
            ""
        }
        Log.i(TAG, "Loading $base (attempt $loadAttempt)")
        statusView?.text = "Connecting to $base …"
        wv.loadUrl(if (token.isNotEmpty()) "$base?token=${Uri.encode(token)}" else base)
        handler.postDelayed(loadRetryRunnable, 5000)
    }

    private fun hasPermission(permission: String): Boolean =
        ContextCompat.checkSelfPermission(this, permission) == PackageManager.PERMISSION_GRANTED

    /** Call a global JS callback in the UI with an already-encoded JS argument. */
    private fun deliverToWeb(function: String, jsArg: String) {
        runOnUiThread {
            webView?.evaluateJavascript(
                "window.$function && window.$function($jsArg);",
                null
            )
        }
    }

    // ── Pairing: QR scanner, share sheet, NFC reader mode ──

    fun launchQrScanner() {
        val options = ScanOptions()
            .setDesiredBarcodeFormats(ScanOptions.QR_CODE)
            .setPrompt("Scan the SRLTCP pairing code on the other device")
            .setBeepEnabled(false)
            .setOrientationLocked(false)
        try {
            qrScanLauncher.launch(options)
        } catch (e: Exception) {
            Log.e(TAG, "QR scanner failed", e)
        }
    }

    /** Ask where to save a transfer's file, then copy it there. Returns "" or an error. */
    fun saveTransferFile(transferId: String): String {
        val info = try {
            JSONObject(
                Python.getInstance().getModule("srltcp.app")
                    .callAttr("android_transfer_file", transferId).toString()
            )
        } catch (e: Exception) {
            Log.e(TAG, "Transfer lookup failed", e)
            return "Could not open that file"
        }
        if (info.has("error")) return info.getString("error")
        val intent = Intent(Intent.ACTION_CREATE_DOCUMENT).apply {
            addCategory(Intent.CATEGORY_OPENABLE)
            type = info.optString("mime", "application/octet-stream")
            putExtra(Intent.EXTRA_TITLE, info.optString("filename", "file"))
        }
        runOnUiThread {
            pendingSavePath = info.getString("path")
            try {
                saveFileLauncher.launch(intent)
            } catch (e: Exception) {
                pendingSavePath = null
                Log.e(TAG, "No document picker", e)
                deliverToWeb("onAndroidFileSaved", "false")
            }
        }
        return ""
    }

    // ── Call audio: route like a phone call (earpiece / speaker) ──

    private var callAudioActive = false
    private var callSpeakerWanted = false
    private var callDeviceCallback: android.media.AudioDeviceCallback? = null
    private val reapplyCallRoute = Runnable {
        if (callAudioActive) applyCallRoute(getSystemService(AUDIO_SERVICE) as android.media.AudioManager)
    }

    fun setCallAudio(active: Boolean, speaker: Boolean) {
        val am = getSystemService(AUDIO_SERVICE) as android.media.AudioManager
        try {
            if (!active) {
                if (!callAudioActive) return
                callAudioActive = false
                handler.removeCallbacks(reapplyCallRoute)
                callDeviceCallback?.let { am.unregisterAudioDeviceCallback(it) }
                callDeviceCallback = null
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                    am.clearCommunicationDevice()
                }
                @Suppress("DEPRECATION")
                am.isSpeakerphoneOn = false
                am.mode = android.media.AudioManager.MODE_NORMAL
                volumeControlStream = android.media.AudioManager.USE_DEFAULT_STREAM_TYPE
                window.clearFlags(android.view.WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
                return
            }
            callAudioActive = true
            callSpeakerWanted = speaker
            volumeControlStream = android.media.AudioManager.STREAM_VOICE_CALL
            window.addFlags(android.view.WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
            applyCallRoute(am)
            // The WebView's WebRTC audio opens its output stream when the remote
            // audio starts playing and some phones reset the route then; apply
            // it again shortly after, and whenever a headset comes or goes.
            handler.removeCallbacks(reapplyCallRoute)
            handler.postDelayed(reapplyCallRoute, 700)
            handler.postDelayed(reapplyCallRoute, 2500)
            if (callDeviceCallback == null) {
                val cb = object : android.media.AudioDeviceCallback() {
                    override fun onAudioDevicesAdded(added: Array<out android.media.AudioDeviceInfo>?) {
                        handler.postDelayed(reapplyCallRoute, 300)
                    }
                    override fun onAudioDevicesRemoved(removed: Array<out android.media.AudioDeviceInfo>?) {
                        handler.postDelayed(reapplyCallRoute, 300)
                    }
                }
                am.registerAudioDeviceCallback(cb, handler)
                callDeviceCallback = cb
            }
        } catch (e: Exception) {
            Log.w(TAG, "Call audio routing failed", e)
        }
    }

    /** Earpiece (or headset) vs loudspeaker. Uses setCommunicationDevice on
     *  Android 12+, falling back to setSpeakerphoneOn where a phone refuses it. */
    private fun applyCallRoute(am: android.media.AudioManager) {
        val speaker = callSpeakerWanted
        try {
            if (am.mode != android.media.AudioManager.MODE_IN_COMMUNICATION) {
                am.mode = android.media.AudioManager.MODE_IN_COMMUNICATION
            }
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                val devices = am.availableCommunicationDevices
                val headset = devices.firstOrNull {
                    it.type == android.media.AudioDeviceInfo.TYPE_BLUETOOTH_SCO ||
                        it.type == android.media.AudioDeviceInfo.TYPE_WIRED_HEADSET ||
                        it.type == android.media.AudioDeviceInfo.TYPE_WIRED_HEADPHONES ||
                        it.type == android.media.AudioDeviceInfo.TYPE_USB_HEADSET ||
                        it.type == android.media.AudioDeviceInfo.TYPE_BLE_HEADSET
                }
                val wanted = if (speaker) android.media.AudioDeviceInfo.TYPE_BUILTIN_SPEAKER
                else android.media.AudioDeviceInfo.TYPE_BUILTIN_EARPIECE
                val target = if (!speaker && headset != null) headset
                else devices.firstOrNull { it.type == wanted }
                val ok = target != null && am.setCommunicationDevice(target) &&
                    am.communicationDevice?.type == target.type
                if (!ok) {
                    Log.w(TAG, "setCommunicationDevice(${target?.type}) refused — using speakerphone switch")
                    am.clearCommunicationDevice()
                    @Suppress("DEPRECATION")
                    am.isSpeakerphoneOn = speaker
                }
            } else {
                @Suppress("DEPRECATION")
                am.isSpeakerphoneOn = speaker
            }
        } catch (e: Exception) {
            Log.w(TAG, "Call audio routing failed", e)
        }
    }

    fun shareText(title: String, text: String) {
        val send = Intent(Intent.ACTION_SEND).apply {
            type = "text/plain"
            putExtra(Intent.EXTRA_TEXT, text)
        }
        startActivity(Intent.createChooser(send, title))
    }

    fun isNfcAvailable(): Boolean = NfcAdapter.getDefaultAdapter(this) != null

    // NFC tap-to-pair. Two phones cannot both be readers at once, so while the
    // Pair screen is open each phone randomly alternates between reading
    // (reader mode) and being read (host card emulation). Held back to back,
    // the phones line up within a second or two and swap contact cards.
    private var nfcAuto = false
    private var nfcWanted = false  // the Pair screen asked for scanning; survives onPause
    @Volatile private var nfcExchanging = false
    private var nfcReaderOn = false
    private val nfcRandom = java.util.Random()
    private val nfcCycle = object : Runnable {
        override fun run() {
            if (!nfcAuto) return
            if (!nfcExchanging) {
                if (nfcReaderOn) disableReader() else enableReader()
            }
            handler.postDelayed(this, 500L + nfcRandom.nextInt(600))
        }
    }

    private fun enableReader() {
        val adapter = NfcAdapter.getDefaultAdapter(this) ?: return
        try {
            adapter.enableReaderMode(
                this,
                { tag: Tag -> onNfcTag(tag) },
                NfcAdapter.FLAG_READER_NFC_A or NfcAdapter.FLAG_READER_NFC_B or
                    NfcAdapter.FLAG_READER_SKIP_NDEF_CHECK or NfcAdapter.FLAG_READER_NO_PLATFORM_SOUNDS,
                null
            )
            nfcReaderOn = true
        } catch (e: Exception) {
            Log.w(TAG, "enableReaderMode failed", e)
        }
    }

    private fun disableReader() {
        try {
            NfcAdapter.getDefaultAdapter(this)?.disableReaderMode(this)
        } catch (_: Exception) { /* ignore */ }
        nfcReaderOn = false
    }

    private fun onNfcTag(tag: Tag) {
        nfcExchanging = true
        try {
            val result = NfcPairing.exchangeAsReader(tag)
            val json = try { JSONObject(result) } catch (_: Exception) { JSONObject() }
            if (json.optBoolean("retry", false) && nfcAuto) return  // keep cycling quietly
            if (!json.has("error")) {
                vibrate(longArrayOf(0, 60, 80, 60))
                runOnUiThread { stopNfcReader() }
            }
            deliverToWeb("onAndroidNfcResult", JSONObject.quote(result))
        } finally {
            nfcExchanging = false
        }
    }

    fun vibrate(pattern: LongArray) {
        try {
            val vib = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                (getSystemService(android.os.VibratorManager::class.java))?.defaultVibrator
            } else {
                @Suppress("DEPRECATION")
                getSystemService(android.content.Context.VIBRATOR_SERVICE) as? android.os.Vibrator
            }
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                vib?.vibrate(android.os.VibrationEffect.createWaveform(pattern, -1))
            } else {
                @Suppress("DEPRECATION")
                vib?.vibrate(pattern, -1)
            }
        } catch (_: Exception) { /* no vibrator */ }
    }

    /**
     * Start automatic NFC pairing. Returns "ok", "off" (NFC disabled) or
     * "missing" (no NFC hardware).
     */
    fun startNfcReader(): String {
        val adapter = NfcAdapter.getDefaultAdapter(this) ?: return "missing"
        if (!adapter.isEnabled) return "off"
        runOnUiThread {
            nfcWanted = true
            startNfcCycle()
        }
        return "ok"
    }

    private fun startNfcCycle() {
        if (nfcAuto) return
        nfcAuto = true
        handler.removeCallbacks(nfcCycle)
        // Random start phase so two phones opened together do not stay in sync.
        handler.postDelayed(nfcCycle, nfcRandom.nextInt(700).toLong())
    }

    /** Stop scanning for good (Pair screen closed or paired). */
    fun stopNfcReader() {
        nfcWanted = false
        pauseNfcReader()
    }

    /** Stop scanning but restart it in onResume if the Pair screen still wants it. */
    private fun pauseNfcReader() {
        nfcAuto = false
        handler.removeCallbacks(nfcCycle)
        disableReader()
    }

    fun openNfcSettings() {
        try {
            startActivity(Intent(Settings.ACTION_NFC_SETTINGS))
        } catch (_: Exception) {
            startActivity(Intent(Settings.ACTION_WIRELESS_SETTINGS))
        }
    }

    private fun isOurUrl(url: String?): Boolean {
        val uri = Uri.parse(url ?: return false)
        return uri.scheme == "https" && uri.host == "127.0.0.1"
    }

    private var pinnedCertSha256: String? = null

    /** Accept only the certificate generated by our own Python server. */
    private fun isOurCertificate(error: SslError): Boolean {
        return try {
            val expected = pinnedCertSha256 ?: Python.getInstance()
                .getModule("srltcp.app")
                .callAttr("get_android_cert_sha256")
                .toString()
                .lowercase()
                .also { pinnedCertSha256 = it }
            val der = SslCertificate.saveState(error.certificate)
                .getByteArray("x509-certificate") ?: return false
            val actual = MessageDigest.getInstance("SHA-256").digest(der)
                .joinToString("") { "%02x".format(it) }
            actual == expected
        } catch (e: Exception) {
            Log.e(TAG, "Certificate pin check failed", e)
            false
        }
    }

    private fun resolvePort(): Int {
        return try {
            val py = Python.getInstance()
            if (py.getModule("srltcp.app").callAttr("is_android_server_ready").toBoolean()) {
                py.getModule("srltcp.app").callAttr("get_android_web_port").toInt()
            } else {
                9876
            }
        } catch (_: Exception) {
            9876
        }
    }

    private fun showFatal(message: String) {
        handler.post {
            webView?.let {
                try {
                    (it.parent as? FrameLayout)?.removeView(it)
                } catch (_: Exception) { /* ignore */ }
            }
            webView = null
            val tv = TextView(this).apply {
                text = "SRLTCP\n\n$message"
                setTextColor(0xFFEEEEEE.toInt())
                setBackgroundColor(0xFF0C0E14.toInt())
                setPadding(48, 48, 48, 48)
                textSize = 16f
            }
            setContentView(tv)
        }
    }

    override fun onPause() {
        isActivityResumed = false
        NfcPairing.active = false
        pauseNfcReader()
        super.onPause()
        webView?.onPause()
        if (isFinishing) {
            prepareForClose()
        }
    }

    override fun onStop() {
        if (isFinishing) {
            prepareForClose()
        } else {
            maybeStartForegroundService()
        }
        super.onStop()
    }

    override fun onResume() {
        super.onResume()
        isActivityResumed = true
        if (isClosing) return
        NfcPairing.active = true
        if (nfcWanted && NfcAdapter.getDefaultAdapter(this)?.isEnabled == true) startNfcCycle()
        webView?.onResume()
        webView?.evaluateJavascript("window.srltcpOnResume && window.srltcpOnResume();", null)
        startPythonServer()  // no-op while the node is running
        if (webView?.url.isNullOrBlank() &&
            statusView?.visibility == android.view.View.VISIBLE &&
            isServerReady()
        ) {
            scheduleLoad(300)
        }
    }

    private fun isServerReady(): Boolean {
        return try {
            Python.getInstance()
                .getModule("srltcp.app")
                .callAttr("is_android_server_ready")
                .toBoolean()
        } catch (_: Exception) {
            false
        }
    }

    private fun prepareForClose() {
        if (isClosing) return
        isClosing = true
        isTrustedPage = false
        pageLoaded = false
        handler.removeCallbacks(loadRetryRunnable)
        handler.removeCallbacksAndMessages(null)
        webView?.let { wv ->
            try {
                wv.stopLoading()
                wv.loadUrl("about:blank")
                wv.visibility = android.view.View.GONE
            } catch (_: Exception) { /* ignore */ }
        }
        statusView?.visibility = android.view.View.GONE
    }

    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        webView?.saveState(outState)
        pendingSavePath?.let { outState.putString(STATE_PENDING_SAVE, it) }
    }

    override fun onDestroy() {
        setCallAudio(false, false)
        NfcPairing.listener = null
        NfcPairing.card = null
        NfcPairing.active = false
        if (isFinishing) {
            prepareForClose()
        }
        isTrustedPage = false
        handler.removeCallbacksAndMessages(null)
        // Destroy the WebView on recreation too, or the old one (and a call in
        // it) keeps running. The node itself keeps running after the UI closes,
        // like any messenger; quit from the notification to stop it.
        webView?.let { wv ->
            try {
                wv.removeJavascriptInterface("SRLTCPAndroid")
                wv.stopLoading()
                (wv.parent as? FrameLayout)?.removeView(wv)
                wv.destroy()
            } catch (_: Exception) { /* ignore */ }
        }
        webView = null
        super.onDestroy()
    }

    companion object {
        private const val TAG = "SRLTCP"
        private const val PREFS = "srltcp_prefs"
        private const val PREF_ALL_FILES_ASKED = "all_files_access_asked"
        private const val STATE_PENDING_SAVE = "pending_save_path"

        /** The open activity, so Quit can close the screen before the process ends. */
        @Volatile
        var current: java.lang.ref.WeakReference<MainActivity>? = null
    }
}
