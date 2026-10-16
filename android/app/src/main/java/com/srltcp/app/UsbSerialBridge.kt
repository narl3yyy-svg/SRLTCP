package com.srltcp.app

import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.hardware.usb.UsbManager
import android.os.Build
import android.util.Log
import com.hoho.android.usbserial.driver.UsbSerialDriver
import com.hoho.android.usbserial.driver.UsbSerialPort
import com.hoho.android.usbserial.driver.UsbSerialProber
import org.json.JSONArray
import org.json.JSONObject

/**
 * USB serial adapters (FTDI, CP210x, CH34x, PL2303, CDC-ACM boards like
 * Arduino/ESP32/RP2040, radio modems) for the Python serial transport.
 *
 * Android gives apps no /dev/ttyUSB* access: USB devices are reached through
 * the USB host API, one app at a time, after the user allows it. Python calls
 * these methods through Chaquopy (see srltcp/transports/android_serial.py).
 * Device ids look like "usb:0403:6001#0" (vendor id, product id, port
 * index): unlike the /dev/bus/usb path they stay the same when the adapter
 * is plugged in again, so the choice in Settings keeps working.
 */
object UsbSerialBridge {
    private const val TAG = "SRLTCP-USB"
    const val ACTION_PERMISSION = "com.srltcp.app.USB_PERMISSION"

    private var appContext: Context? = null
    private var port: UsbSerialPort? = null
    private var openId: String = ""
    // Each open gets a new number: a closing/old reader can't touch a newer port.
    private var generation = 0
    // Devices (by path, i.e. per plug-in) we already asked the user about: the
    // background retry never asks twice; Save in Settings asks again.
    private val asked = mutableSetOf<String>()

    @JvmStatic
    fun init(context: Context) {
        appContext = context.applicationContext
    }

    private fun usb(): UsbManager? =
        appContext?.getSystemService(Context.USB_SERVICE) as? UsbManager

    /** Known serial chips and any device with a real CDC-ACM interface (the
     *  default prober checks the interface class). Nothing else: a keyboard,
     *  flash drive or USB-C headset must never be claimed as a serial port. */
    private fun drivers(manager: UsbManager): List<UsbSerialDriver> =
        UsbSerialProber.getDefaultProber().findAllDrivers(manager)

    /** JSON list: [{device, description, accessible}] for the settings UI. */
    @JvmStatic
    fun listPorts(): String {
        val out = JSONArray()
        val manager = usb() ?: return out.toString()
        try {
            for (driver in drivers(manager)) {
                val dev = driver.device
                val name = listOfNotNull(dev.manufacturerNameSafe(), dev.productNameSafe())
                    .joinToString(" ").ifBlank { driver.javaClass.simpleName.removeSuffix("SerialDriver") }
                driver.ports.forEachIndexed { index, _ ->
                    val label = buildString {
                        append(name)
                        append(String.format(" (USB %04X:%04X", dev.vendorId, dev.productId))
                        if (driver.ports.size > 1) append(", port ${index + 1}")
                        append(")")
                    }
                    out.put(JSONObject().apply {
                        put("device", String.format("usb:%04x:%04x#%d", dev.vendorId, dev.productId, index))
                        put("description", label)
                        put("accessible", manager.hasPermission(dev))
                    })
                }
            }
        } catch (e: Exception) {
            Log.w(TAG, "Listing USB serial devices failed", e)
        }
        return out.toString()
    }

    private fun find(id: String): Pair<UsbSerialDriver, Int>? {
        val manager = usb() ?: return null
        val ids = id.removePrefix("usb:").substringBeforeLast('#').split(':')
        val vid = ids.getOrNull(0)?.toIntOrNull(16) ?: return null
        val pid = ids.getOrNull(1)?.toIntOrNull(16) ?: return null
        val index = id.substringAfterLast('#', "0").toIntOrNull() ?: 0
        val driver = drivers(manager).firstOrNull {
            it.device.vendorId == vid && it.device.productId == pid
        } ?: return null
        return if (index in driver.ports.indices) driver to index else null
    }

    /** Ask the user to allow this app to use the device (Android shows a dialog). */
    @JvmStatic
    fun requestPermission(id: String): Boolean {
        val ctx = appContext ?: return false
        val manager = usb() ?: return false
        val (driver, _) = find(id) ?: return false
        if (manager.hasPermission(driver.device)) return true
        val flags = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) PendingIntent.FLAG_MUTABLE else 0
        val intent = Intent(ACTION_PERMISSION).setPackage(ctx.packageName)
        manager.requestPermission(driver.device, PendingIntent.getBroadcast(ctx, 0, intent, flags))
        return false
    }

    /** Open the port: the new generation (> 0) on success, else -1 "permission",
     *  -2 "missing", -3 any other failure (see [lastError]). [ask]: show Android's
     *  permission question even if we asked before for this plug-in. */
    @JvmStatic
    @Synchronized
    fun open(id: String, baud: Int, ask: Boolean): Int {
        closeCurrent()
        val manager = usb() ?: return fail("no USB host support")
        val (driver, index) = find(id) ?: return -2
        if (!manager.hasPermission(driver.device)) {
            if (asked.add(driver.device.deviceName) || ask) requestPermission(id)
            return -1
        }
        val connection = manager.openDevice(driver.device) ?: return fail("could not open the device")
        val p = driver.ports[index]
        return try {
            p.open(connection)
            p.setParameters(baud, 8, UsbSerialPort.STOPBITS_1, UsbSerialPort.PARITY_NONE)
            try {
                // Many boards (Arduino, CDC radios) only send with DTR set. RTS is
                // left alone: on ESP32 boards RTS drives the reset/boot lines.
                p.setDTR(true)
            } catch (_: Exception) {
            }
            port = p
            openId = id
            generation += 1
            generation
        } catch (e: Exception) {
            try { p.close() } catch (_: Exception) {}
            connection.close()
            fail(e.message ?: e.javaClass.simpleName)
        }
    }

    @JvmStatic
    var lastError: String = ""
        private set

    private fun fail(reason: String): Int {
        lastError = reason
        return -3
    }

    private fun current(gen: Int): UsbSerialPort? = if (gen == generation) port else null

    @JvmStatic
    fun isOpen(gen: Int): Boolean = current(gen)?.isOpen == true

    /** Up to [max] bytes, waiting at most [timeoutMs]; empty if nothing came.
     *  Throws when the device is gone (unplugged) or this open was replaced. */
    @JvmStatic
    fun read(gen: Int, max: Int, timeoutMs: Int): ByteArray {
        val p = current(gen) ?: throw IllegalStateException("not open")
        val buf = ByteArray(maxOf(max, p.readEndpoint?.maxPacketSize ?: 64))
        val n = p.read(buf, timeoutMs)
        return if (n <= 0) ByteArray(0) else buf.copyOf(n)
    }

    @JvmStatic
    fun write(gen: Int, data: ByteArray, timeoutMs: Int) {
        val p = current(gen) ?: throw IllegalStateException("not open")
        p.write(data, timeoutMs)
    }

    /** Close the port opened as [gen] (nothing if a newer one replaced it). */
    @JvmStatic
    @Synchronized
    fun close(gen: Int) {
        if (gen == generation) closeCurrent()
    }

    private fun closeCurrent() {
        val p = port ?: return
        port = null
        openId = ""
        try { p.close() } catch (_: Exception) {}
    }

    private fun android.hardware.usb.UsbDevice.manufacturerNameSafe(): String? =
        try { manufacturerName } catch (_: SecurityException) { null }

    private fun android.hardware.usb.UsbDevice.productNameSafe(): String? =
        try { productName } catch (_: SecurityException) { null }
}
