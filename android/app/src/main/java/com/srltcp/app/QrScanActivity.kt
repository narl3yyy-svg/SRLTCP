package com.srltcp.app

import android.annotation.SuppressLint
import android.app.Activity
import android.graphics.Color
import android.graphics.Rect
import android.hardware.Camera
import android.os.Bundle
import android.util.TypedValue
import android.view.Gravity
import android.view.KeyEvent
import android.view.MotionEvent
import android.view.ScaleGestureDetector
import android.view.View
import android.view.ViewGroup
import android.widget.Button
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.SeekBar
import android.widget.TextView
import com.journeyapps.barcodescanner.CameraPreview
import com.journeyapps.barcodescanner.CaptureManager
import com.journeyapps.barcodescanner.DecoratedBarcodeView
import com.journeyapps.barcodescanner.camera.CameraSettings

/**
 * QR scanner for pairing. Unlike the library's default screen it keeps the
 * camera focusing all the time (instead of a refocus every 2 s), focuses where
 * you tap, and starts zoomed in a little: phones can't focus closer than about
 * 10 cm, so a small zoom lets you hold the phone farther back where it can.
 * Pinch or the slider to zoom; the button turns the flashlight on.
 */
@Suppress("DEPRECATION") // the library drives the classic Camera API
class QrScanActivity : Activity() {
    private lateinit var capture: CaptureManager
    private lateinit var scanner: DecoratedBarcodeView
    private lateinit var zoomBar: SeekBar
    private var maxZoom = 0
    private var torchOn = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        scanner = DecoratedBarcodeView(this)
        setContentView(buildLayout())
        capture = CaptureManager(this, scanner)
        capture.initializeFromIntent(intent, savedInstanceState)
        // initializeFromIntent sets fresh camera settings: adjust them after it.
        scanner.cameraSettings = scanner.cameraSettings.apply {
            isAutoFocusEnabled = true
            isContinuousFocusEnabled = true
            focusMode = CameraSettings.FocusMode.CONTINUOUS
            isMeteringEnabled = true
            isBarcodeSceneModeEnabled = false
            isExposureEnabled = false
        }
        scanner.barcodeView.addStateListener(object : CameraPreview.StateListener {
            override fun previewSized() {}
            override fun previewStarted() = setUpZoom()
            override fun previewStopped() {}
            override fun cameraError(error: Exception) {}
            override fun cameraClosed() {}
        })
        capture.decode()
    }

    private fun dp(value: Int): Int = TypedValue.applyDimension(
        TypedValue.COMPLEX_UNIT_DIP, value.toFloat(), resources.displayMetrics
    ).toInt()

    @SuppressLint("ClickableViewAccessibility")
    private fun buildLayout(): View {
        val root = FrameLayout(this)
        root.addView(scanner, FrameLayout.LayoutParams(MATCH, MATCH))
        scanner.setStatusText("")

        val hint = TextView(this).apply {
            text = "Hold the phone about 20 cm from the code\nTap to focus · pinch to zoom"
            setTextColor(Color.WHITE)
            textSize = 15f
            gravity = Gravity.CENTER
            setShadowLayer(4f, 0f, 1f, Color.BLACK)
        }
        root.addView(hint, FrameLayout.LayoutParams(MATCH, WRAP, Gravity.TOP).apply {
            topMargin = dp(48)
        })

        val controls = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setPadding(dp(16), dp(8), dp(16), dp(24))
            setBackgroundColor(0x99000000.toInt())
        }
        val zoomLabel = TextView(this).apply {
            text = "Zoom"
            setTextColor(Color.WHITE)
        }
        zoomBar = SeekBar(this).apply {
            isEnabled = false
            setOnSeekBarChangeListener(object : SeekBar.OnSeekBarChangeListener {
                override fun onProgressChanged(bar: SeekBar, value: Int, fromUser: Boolean) {
                    if (fromUser) setZoom(value)
                }
                override fun onStartTrackingTouch(bar: SeekBar) {}
                override fun onStopTrackingTouch(bar: SeekBar) {}
            })
        }
        val torch = Button(this).apply {
            text = "Light"
            visibility = if (packageManager.hasSystemFeature("android.hardware.camera.flash")) {
                View.VISIBLE
            } else {
                View.GONE
            }
            setOnClickListener {
                torchOn = !torchOn
                if (torchOn) scanner.setTorchOn() else scanner.setTorchOff()
                text = if (torchOn) "Light off" else "Light"
            }
        }
        controls.addView(zoomLabel)
        controls.addView(zoomBar, LinearLayout.LayoutParams(0, WRAP, 1f))
        controls.addView(torch)
        root.addView(controls, FrameLayout.LayoutParams(MATCH, WRAP, Gravity.BOTTOM))

        val pinch = ScaleGestureDetector(this, object :
            ScaleGestureDetector.SimpleOnScaleGestureListener() {
            override fun onScale(detector: ScaleGestureDetector): Boolean {
                if (maxZoom <= 0) return false
                val step = ((detector.scaleFactor - 1f) * maxZoom * 1.5f).toInt()
                if (step != 0) setZoom(zoomBar.progress + step)
                return true
            }
        })
        scanner.barcodeView.setOnTouchListener { view, event ->
            pinch.onTouchEvent(event)
            if (event.actionMasked == MotionEvent.ACTION_UP && !pinch.isInProgress &&
                event.eventTime - event.downTime < 300
            ) {
                focusAt(event.x / view.width, event.y / view.height)
                view.performClick()
            }
            true
        }
        return root
    }

    /** Start at about 2× (or the most the camera has) once the camera is open. */
    private fun setUpZoom() {
        scanner.changeCameraParameters { params ->
            if (params.isZoomSupported) {
                val max = params.maxZoom
                val ratios = params.zoomRatios ?: emptyList()
                val start = ratios.indexOfFirst { it >= 200 }.let { if (it < 0) max else it }
                params.zoom = start.coerceIn(0, max)
                runOnUiThread {
                    maxZoom = max
                    zoomBar.max = max
                    zoomBar.progress = params.zoom
                    zoomBar.isEnabled = max > 0
                }
            }
            params
        }
    }

    private fun setZoom(value: Int) {
        if (maxZoom <= 0) return
        val zoom = value.coerceIn(0, maxZoom)
        zoomBar.progress = zoom
        scanner.changeCameraParameters { params ->
            if (params.isZoomSupported) params.zoom = zoom.coerceIn(0, params.maxZoom)
            params
        }
    }

    /**
     * Focus (and meter) on the tapped spot. [fx]/[fy] are 0..1 across the view.
     * Re-applying the focus mode makes the camera search again right away.
     */
    private fun focusAt(fx: Float, fy: Float) {
        scanner.changeCameraParameters { params ->
            // Camera areas run from -1000 to 1000 in the sensor's orientation;
            // the preview is rotated for portrait, so swap the axes back.
            val portrait = resources.configuration.orientation ==
                android.content.res.Configuration.ORIENTATION_PORTRAIT
            val (sx, sy) = if (portrait) fy to (1f - fx) else fx to fy
            val cx = (sx * 2000 - 1000).toInt()
            val cy = (sy * 2000 - 1000).toInt()
            val half = 150
            val area = Rect(
                (cx - half).coerceIn(-1000, 1000 - 2 * half),
                (cy - half).coerceIn(-1000, 1000 - 2 * half),
                (cx + half).coerceIn(-1000 + 2 * half, 1000),
                (cy + half).coerceIn(-1000 + 2 * half, 1000),
            )
            val areas = listOf(Camera.Area(area, 1000))
            if (params.maxNumFocusAreas > 0) params.focusAreas = areas
            if (params.maxNumMeteringAreas > 0) params.meteringAreas = areas
            val modes = params.supportedFocusModes ?: emptyList()
            when {
                Camera.Parameters.FOCUS_MODE_CONTINUOUS_PICTURE in modes ->
                    params.focusMode = Camera.Parameters.FOCUS_MODE_CONTINUOUS_PICTURE
                Camera.Parameters.FOCUS_MODE_AUTO in modes ->
                    params.focusMode = Camera.Parameters.FOCUS_MODE_AUTO
            }
            params
        }
    }

    override fun onResume() {
        super.onResume()
        capture.onResume()
    }

    override fun onPause() {
        super.onPause()
        capture.onPause()
    }

    override fun onDestroy() {
        super.onDestroy()
        capture.onDestroy()
    }

    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        capture.onSaveInstanceState(outState)
    }

    override fun onRequestPermissionsResult(
        requestCode: Int, permissions: Array<out String>, grantResults: IntArray
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        capture.onRequestPermissionsResult(requestCode, permissions, grantResults)
    }

    override fun onKeyDown(keyCode: Int, event: KeyEvent?): Boolean =
        scanner.onKeyDown(keyCode, event) || super.onKeyDown(keyCode, event)

    private companion object {
        const val MATCH = ViewGroup.LayoutParams.MATCH_PARENT
        const val WRAP = ViewGroup.LayoutParams.WRAP_CONTENT
    }
}
