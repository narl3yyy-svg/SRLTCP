package com.srltcp.app

import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.hardware.usb.UsbManager
import android.os.Build
import android.util.Log
import com.hoho.android.usbserial.driver.CdcAcmSerialDriver
import com.hoho.android.usbserial.driver.ProbeTable
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

    @JvmStatic
    fun init(context: Context) {
        appContext = context.applicationContext
    }

    private fun usb(): UsbManager? =
        appContext?.getSystemService(Context.USB_SERVICE) as? UsbManager

    /** Known chips, plus any device with a CDC-ACM interface listed by the
     *  default prober; boards with unknown ids that still speak CDC-ACM are
     *  tried as CDC-ACM too. */
    private fun drivers(manager: UsbManager): List<UsbSerialDriver> {
        val found = UsbSerialProber.getDefaultProber().findAllDrivers(manager).toMutableList()
        val known = found.map { it.device.deviceName }.toSet()
        for (device in manager.deviceList.values) {
            if (device.deviceName in known) continue
            val table = ProbeTable().addProduct(device.vendorId, device.productId, CdcAcmSerialDriver::class.java)
            val driver = UsbSerialProber(table).probeDevice(device) ?: continue
            if (driver.ports.isNotEmpty()) found.add(driver)
        }
        return found
    }

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

    /** Open the port: "" on success, else a short reason ("permission", "missing", or an error). */
    @JvmStatic
    @Synchronized
    fun open(id: String, baud: Int): String {
        close()
        val manager = usb() ?: return "no USB host support"
        val (driver, index) = find(id) ?: return "missing"
        if (!manager.hasPermission(driver.device)) {
            requestPermission(id)
            return "permission"
        }
        val connection = manager.openDevice(driver.device) ?: return "could not open the device"
        val p = driver.ports[index]
        return try {
            p.open(connection)
            p.setParameters(baud, 8, UsbSerialPort.STOPBITS_1, UsbSerialPort.PARITY_NONE)
            try {
                p.setDTR(true)  // many boards (Arduino, ESP32) only talk with DTR/RTS set
                p.setRTS(true)
            } catch (_: Exception) {
            }
            port = p
            openId = id
            ""
        } catch (e: Exception) {
            try { p.close() } catch (_: Exception) {}
            connection.close()
            e.message ?: e.javaClass.simpleName
        }
    }

    @JvmStatic
    fun isOpen(): Boolean = port?.isOpen == true

    /** Up to [max] bytes, waiting at most [timeoutMs]; empty if nothing came.
     *  Throws when the device is gone (unplugged). */
    @JvmStatic
    fun read(max: Int, timeoutMs: Int): ByteArray {
        val p = port ?: throw IllegalStateException("not open")
        val buf = ByteArray(maxOf(max, p.readEndpoint?.maxPacketSize ?: 64))
        val n = p.read(buf, timeoutMs)
        return if (n <= 0) ByteArray(0) else buf.copyOf(n)
    }

    @JvmStatic
    fun write(data: ByteArray, timeoutMs: Int) {
        val p = port ?: throw IllegalStateException("not open")
        p.write(data, timeoutMs)
    }

    @JvmStatic
    @Synchronized
    fun close() {
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
