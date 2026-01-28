package com.claudewatch.app

import android.Manifest
import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.Intent
import android.content.pm.PackageManager
import android.media.MediaPlayer
import android.media.MediaRecorder
import android.os.Build
import android.os.Bundle
import android.os.PowerManager
import android.os.VibrationEffect
import android.os.Vibrator
import android.util.Log
import android.view.View
import android.widget.Button
import android.widget.ImageButton
import android.widget.LinearLayout
import android.widget.ProgressBar
import android.widget.TextView
import android.app.Activity
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import androidx.core.app.ActivityCompat
import kotlinx.coroutines.*
import okhttp3.*
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.RequestBody.Companion.asRequestBody
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.io.File
import java.io.IOException
import java.util.concurrent.TimeUnit

class MainActivity : Activity() {

    companion object {
        private const val TAG = "ClaudeWatch"
        private const val PERMISSION_REQUEST_CODE = 1001
        private const val NOTIFICATION_PERMISSION_CODE = 1002
        private const val CHANNEL_ID = "claude_response"
        private const val NOTIFICATION_ID = 1
        private const val POLL_INTERVAL_MS = 5000L  // 5 seconds
        private const val MAX_POLL_ATTEMPTS = 24     // 2 minutes max
        private const val WS_RECONNECT_DELAY_MS = 5000L
    }

    // UI elements
    private lateinit var recordButton: Button
    private lateinit var abortButton: Button
    private lateinit var audioControls: LinearLayout
    private lateinit var replayButton: Button
    private lateinit var pauseButton: Button
    private lateinit var doneButton: Button
    private lateinit var settingsButton: ImageButton
    private lateinit var statusText: TextView
    private lateinit var progressBar: ProgressBar

    // Permission prompt UI
    private lateinit var permissionPrompt: LinearLayout
    private lateinit var permissionTitle: TextView
    private lateinit var permissionQuestion: TextView
    private lateinit var allowButton: Button
    private lateinit var denyButton: Button

    // State
    private var mediaRecorder: MediaRecorder? = null
    private var mediaPlayer: MediaPlayer? = null
    private var audioFile: File? = null
    private var isRecording = false
    private var isWaitingForResponse = false
    private var isPlayingAudio = false
    private var currentAudioFile: File? = null
    private var currentRequestId: String? = null
    private var pollingJob: Job? = null
    private var wakeLock: PowerManager.WakeLock? = null

    // WebSocket state
    private var webSocket: WebSocket? = null
    private var wsReconnectJob: Job? = null
    private var currentPermissionRequestId: String? = null

    private val coroutineScope = CoroutineScope(Dispatchers.Main + Job())
    private val httpClient = OkHttpClient.Builder()
        .connectTimeout(30, TimeUnit.SECONDS)
        .writeTimeout(60, TimeUnit.SECONDS)
        .readTimeout(30, TimeUnit.SECONDS)
        .build()

    private val wsClient = OkHttpClient.Builder()
        .readTimeout(0, TimeUnit.MILLISECONDS)
        .pingInterval(30, TimeUnit.SECONDS)
        .build()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        // Initialize UI elements
        recordButton = findViewById(R.id.recordButton)
        abortButton = findViewById(R.id.abortButton)
        audioControls = findViewById(R.id.audioControls)
        replayButton = findViewById(R.id.replayButton)
        pauseButton = findViewById(R.id.pauseButton)
        doneButton = findViewById(R.id.doneButton)
        settingsButton = findViewById(R.id.settingsButton)
        statusText = findViewById(R.id.statusText)
        progressBar = findViewById(R.id.progressBar)

        // Permission prompt UI
        permissionPrompt = findViewById(R.id.permissionPrompt)
        permissionTitle = findViewById(R.id.permissionTitle)
        permissionQuestion = findViewById(R.id.permissionQuestion)
        allowButton = findViewById(R.id.allowButton)
        denyButton = findViewById(R.id.denyButton)

        // Set up click listeners
        recordButton.setOnClickListener { onRecordButtonClick() }
        abortButton.setOnClickListener { onAbortClick() }
        replayButton.setOnClickListener { onReplayClick() }
        pauseButton.setOnClickListener { onPauseClick() }
        doneButton.setOnClickListener { onDoneClick() }
        settingsButton.setOnClickListener {
            startActivity(Intent(this, SettingsActivity::class.java))
        }
        allowButton.setOnClickListener { respondToPermission("allow") }
        denyButton.setOnClickListener { respondToPermission("deny") }

        createNotificationChannel()
        requestNotificationPermission()

        // Connect to WebSocket for permission prompts
        connectWebSocket()

        // Auto-start recording on launch (only if not waiting)
        autoStartRecording()
    }

    private fun requestNotificationPermission() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            if (ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED) {
                ActivityCompat.requestPermissions(
                    this,
                    arrayOf(Manifest.permission.POST_NOTIFICATIONS),
                    NOTIFICATION_PERMISSION_CODE
                )
            }
        }
    }

    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val name = "Claude Responses"
            val descriptionText = "Notifications for Claude responses"
            val importance = NotificationManager.IMPORTANCE_HIGH
            val channel = NotificationChannel(CHANNEL_ID, name, importance).apply {
                description = descriptionText
                enableVibration(true)
            }
            val notificationManager = getSystemService(NotificationManager::class.java)
            notificationManager.createNotificationChannel(channel)
        }
    }

    override fun onNewIntent(intent: Intent?) {
        super.onNewIntent(intent)
        // Button press behavior depends on state
        when {
            isPlayingAudio -> {
                // Toggle pause/play
                onPauseClick()
            }
            isWaitingForResponse -> {
                // Abort waiting
                onAbortClick()
            }
            isRecording -> {
                // Stop and send
                stopRecordingAndSend()
            }
            else -> {
                // Start recording
                autoStartRecording()
            }
        }
    }

    private fun autoStartRecording() {
        // Don't start recording if waiting for response or playing audio
        if (isWaitingForResponse || isPlayingAudio) {
            Log.d(TAG, "Skipping auto-record: waiting=$isWaitingForResponse, playing=$isPlayingAudio")
            return
        }

        if (checkPermission()) {
            if (!isRecording) {
                startRecording()
            }
        } else {
            ActivityCompat.requestPermissions(
                this,
                arrayOf(Manifest.permission.RECORD_AUDIO),
                PERMISSION_REQUEST_CODE
            )
        }
    }

    override fun onDestroy() {
        super.onDestroy()
        pollingJob?.cancel()
        wsReconnectJob?.cancel()
        webSocket?.close(1000, "Activity destroyed")
        coroutineScope.cancel()
        releaseWakeLock()
        stopRecording()
        mediaPlayer?.release()
        mediaPlayer = null
        currentAudioFile?.delete()
        httpClient.dispatcher.executorService.shutdown()
        wsClient.dispatcher.executorService.shutdown()
    }

    // WebSocket connection for permission prompts
    private fun connectWebSocket() {
        val wsUrl = SettingsActivity.getWebSocketUrl(this)
        Log.d(TAG, "Connecting WebSocket to $wsUrl")

        val request = Request.Builder()
            .url(wsUrl)
            .build()

        webSocket = wsClient.newWebSocket(request, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                Log.i(TAG, "WebSocket connected")
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                Log.d(TAG, "WebSocket message: $text")
                handleWebSocketMessage(text)
            }

            override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                Log.i(TAG, "WebSocket closing: $code $reason")
                webSocket.close(1000, null)
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                Log.i(TAG, "WebSocket closed: $code $reason")
                scheduleReconnect()
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                Log.e(TAG, "WebSocket error", t)
                scheduleReconnect()
            }
        })
    }

    private fun scheduleReconnect() {
        wsReconnectJob?.cancel()
        wsReconnectJob = coroutineScope.launch {
            delay(WS_RECONNECT_DELAY_MS)
            connectWebSocket()
        }
    }

    private fun handleWebSocketMessage(text: String) {
        try {
            val json = JSONObject(text)
            when (json.optString("type")) {
                "permission" -> {
                    val requestId = json.optString("request_id", "")
                    val toolName = json.optString("tool_name", "")
                    val question = json.optString("question", "Allow action?")

                    if (requestId.isNotEmpty()) {
                        currentPermissionRequestId = requestId
                        runOnUiThread {
                            showPermissionPrompt(toolName, question)
                        }
                    }
                }
                "permission_resolved" -> {
                    val resolvedId = json.optString("request_id", "")
                    if (resolvedId == currentPermissionRequestId) {
                        currentPermissionRequestId = null
                        runOnUiThread {
                            hidePermissionPrompt()
                        }
                    }
                }
            }
        } catch (e: Exception) {
            Log.e(TAG, "Error parsing WebSocket message", e)
        }
    }

    private fun showPermissionPrompt(toolName: String, question: String) {
        permissionTitle.text = toolName.ifEmpty { "Permission" }
        permissionQuestion.text = question
        permissionPrompt.visibility = View.VISIBLE
        vibrate(longArrayOf(0, 100, 50, 100))
    }

    private fun hidePermissionPrompt() {
        permissionPrompt.visibility = View.GONE
    }

    private fun respondToPermission(decision: String) {
        val requestId = currentPermissionRequestId ?: return

        hidePermissionPrompt()
        vibrate(50)

        coroutineScope.launch {
            try {
                withContext(Dispatchers.IO) {
                    val baseUrl = SettingsActivity.getBaseUrl(this@MainActivity)
                    val url = "$baseUrl/api/permission/respond"

                    val jsonBody = JSONObject().apply {
                        put("request_id", requestId)
                        put("decision", decision)
                    }

                    val requestBody = jsonBody.toString()
                        .toByteArray()
                        .toRequestBody("application/json".toMediaType())

                    val request = Request.Builder()
                        .url(url)
                        .post(requestBody)
                        .build()

                    val response = httpClient.newCall(request).execute()
                    Log.d(TAG, "Permission response sent: ${response.code}")
                }
            } catch (e: Exception) {
                Log.e(TAG, "Error sending permission response", e)
            }
        }

        currentPermissionRequestId = null
    }

    private fun onRecordButtonClick() {
        if (checkPermission()) {
            toggleRecording()
        } else {
            ActivityCompat.requestPermissions(
                this,
                arrayOf(Manifest.permission.RECORD_AUDIO),
                PERMISSION_REQUEST_CODE
            )
        }
    }

    private fun onAbortClick() {
        Log.d(TAG, "Aborting wait for response")
        pollingJob?.cancel()
        pollingJob = null
        isWaitingForResponse = false
        currentRequestId = null
        releaseWakeLock()
        showStatus("Cancelled")
        vibrate(50)
        updateUIState()
    }

    private fun onPauseClick() {
        mediaPlayer?.let { player ->
            if (player.isPlaying) {
                player.pause()
                pauseButton.text = "Play"
                showStatus("Paused")
            } else {
                player.start()
                pauseButton.text = "Pause"
                showStatus("Playing...")
            }
        }
    }

    private fun onReplayClick() {
        currentAudioFile?.let { file ->
            if (file.exists()) {
                playAudioFile(file)
            }
        }
    }

    private fun onDoneClick() {
        // Clean up and return to normal state
        mediaPlayer?.release()
        mediaPlayer = null
        currentAudioFile?.delete()
        currentAudioFile = null
        isPlayingAudio = false

        // Send ack if we have a request ID
        currentRequestId?.let { sendAck(it) }
        currentRequestId = null

        showStatus("Tap to record")
        updateUIState()
    }

    override fun onRequestPermissionsResult(
        requestCode: Int,
        permissions: Array<out String>,
        grantResults: IntArray
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == PERMISSION_REQUEST_CODE) {
            if (grantResults.isNotEmpty() && grantResults[0] == PackageManager.PERMISSION_GRANTED) {
                startRecording()
            } else {
                showStatus("Microphone permission denied", isError = true)
            }
        }
    }

    private fun checkPermission(): Boolean {
        return ContextCompat.checkSelfPermission(
            this,
            Manifest.permission.RECORD_AUDIO
        ) == PackageManager.PERMISSION_GRANTED
    }

    private fun toggleRecording() {
        if (isRecording) {
            stopRecordingAndSend()
        } else {
            startRecording()
        }
    }

    private fun startRecording() {
        try {
            audioFile = File.createTempFile("recording_", ".m4a", cacheDir)

            mediaRecorder = MediaRecorder().apply {
                setAudioSource(MediaRecorder.AudioSource.MIC)
                setOutputFormat(MediaRecorder.OutputFormat.MPEG_4)
                setAudioEncoder(MediaRecorder.AudioEncoder.AAC)
                setAudioEncodingBitRate(128000)
                setAudioSamplingRate(44100)
                setOutputFile(audioFile?.absolutePath)
                prepare()
                start()
            }

            isRecording = true
            vibrate(50)
            updateUIState()
            showStatus("Recording...")
            Log.d(TAG, "Recording started: ${audioFile?.absolutePath}")

        } catch (e: Exception) {
            Log.e(TAG, "Failed to start recording", e)
            showStatus("Failed to start recording", isError = true)
            cleanupRecording()
        }
    }

    private fun stopRecordingAndSend() {
        stopRecording()
        vibrate(100)
        sendRecording()
    }

    private fun stopRecording() {
        try {
            mediaRecorder?.apply {
                stop()
                release()
            }
        } catch (e: Exception) {
            Log.e(TAG, "Error stopping recording", e)
        }
        mediaRecorder = null
        isRecording = false
        updateUIState()
    }

    private fun sendRecording() {
        val file = audioFile ?: run {
            showStatus("No recording to send", isError = true)
            return
        }

        showStatus("Sending...")
        progressBar.visibility = View.VISIBLE

        coroutineScope.launch {
            try {
                val result = withContext(Dispatchers.IO) {
                    sendToServer(file)
                }

                if (result.isSuccess) {
                    val responseBody = result.getOrNull() ?: ""
                    try {
                        val json = JSONObject(responseBody)
                        val requestId = json.optString("request_id", "")
                        val responseEnabled = json.optBoolean("response_enabled", false)

                        if (requestId.isNotEmpty() && responseEnabled) {
                            // Enter waiting state
                            isWaitingForResponse = true
                            currentRequestId = requestId
                            updateUIState()
                            showStatus("Waiting for Claude...")
                            pollForResponse(requestId)
                        } else {
                            showStatus("Sent to Claude")
                            progressBar.visibility = View.GONE
                        }
                    } catch (e: Exception) {
                        showStatus("Sent successfully!")
                        progressBar.visibility = View.GONE
                    }

                    vibrate(50)
                    file.delete()
                    audioFile = null
                } else {
                    showStatus("Failed: ${result.exceptionOrNull()?.message}", isError = true)
                    vibrate(longArrayOf(0, 100, 100, 100))
                    progressBar.visibility = View.GONE
                }
            } catch (e: Exception) {
                Log.e(TAG, "Error sending recording", e)
                showStatus("Error: ${e.message}", isError = true)
                vibrate(longArrayOf(0, 100, 100, 100))
                progressBar.visibility = View.GONE
            }
        }
    }

    private fun pollForResponse(requestId: String) {
        acquireWakeLock()
        pollingJob = coroutineScope.launch {
            var attempts = 0
            while (attempts < MAX_POLL_ATTEMPTS && isActive) {
                val delayMs = if (attempts == 0) 1500L else POLL_INTERVAL_MS
                delay(delayMs)
                attempts++

                try {
                    val result = withContext(Dispatchers.IO) {
                        checkResponse(requestId)
                    }

                    if (result != null) {
                        val status = result.optString("status", "")
                        if (status == "completed") {
                            val response = result.optString("response", "No response")
                            val type = result.optString("type", "text")

                            progressBar.visibility = View.GONE
                            isWaitingForResponse = false
                            releaseWakeLock()

                            if (type == "audio") {
                                val audioUrl = result.optString("audio_url", "")
                                if (audioUrl.isNotEmpty()) {
                                    showStatus("Loading audio...")
                                    downloadAndPlayAudio(audioUrl, requestId)
                                } else {
                                    showNotification(response)
                                    showStatus("Response received!")
                                    sendAck(requestId)
                                    currentRequestId = null
                                    updateUIState()
                                }
                            } else {
                                showNotification(response)
                                showStatus("Response received!")
                                sendAck(requestId)
                                currentRequestId = null
                                updateUIState()
                            }
                            vibrate(longArrayOf(0, 100, 50, 100))
                            return@launch
                        } else if (status == "disabled") {
                            progressBar.visibility = View.GONE
                            isWaitingForResponse = false
                            currentRequestId = null
                            showStatus("Sent to Claude")
                            updateUIState()
                            releaseWakeLock()
                            return@launch
                        } else if (status == "not_found") {
                            Log.w(TAG, "Request not found on server")
                            break
                        }
                        showStatus("Waiting... (${attempts})")
                    }
                } catch (e: Exception) {
                    Log.e(TAG, "Error polling for response", e)
                }
            }

            // Timeout or error
            progressBar.visibility = View.GONE
            isWaitingForResponse = false
            currentRequestId = null
            showStatus("Response timeout")
            updateUIState()
            releaseWakeLock()
        }
    }

    private fun checkResponse(requestId: String): JSONObject? {
        return try {
            val baseUrl = SettingsActivity.getServerUrl(this).replace("/transcribe", "")
            val url = "$baseUrl/api/response/$requestId"

            val request = Request.Builder()
                .url(url)
                .get()
                .build()

            val response = httpClient.newCall(request).execute()

            if (response.isSuccessful) {
                val body = response.body?.string() ?: "{}"
                JSONObject(body)
            } else {
                null
            }
        } catch (e: Exception) {
            Log.e(TAG, "Error checking response", e)
            null
        }
    }

    private fun downloadAndPlayAudio(audioPath: String, requestId: String) {
        coroutineScope.launch {
            try {
                val audioData = withContext(Dispatchers.IO) {
                    downloadAudio(audioPath)
                }

                if (audioData != null) {
                    // Save to temp file
                    val tempFile = File.createTempFile("response_", ".mp3", cacheDir)
                    tempFile.writeBytes(audioData)
                    currentAudioFile = tempFile
                    currentRequestId = requestId

                    // Enter audio playback state
                    isPlayingAudio = true
                    updateUIState()

                    // Play the audio
                    playAudioFile(tempFile)
                } else {
                    showStatus("Audio download failed", isError = true)
                    updateUIState()
                }
            } catch (e: Exception) {
                Log.e(TAG, "Error downloading audio", e)
                showStatus("Audio error", isError = true)
                updateUIState()
            }
        }
    }

    private fun playAudioFile(file: File) {
        mediaPlayer?.release()
        mediaPlayer = MediaPlayer().apply {
            setDataSource(file.absolutePath)
            setOnCompletionListener {
                showStatus("Done - tap to replay")
                pauseButton.text = "Play"
                // Show done button, keep audio controls
                doneButton.visibility = View.VISIBLE
            }
            setOnErrorListener { _, _, _ ->
                showStatus("Audio error", isError = true)
                true
            }
            prepare()
            start()
        }
        pauseButton.text = "Pause"
        showStatus("Playing...")
    }

    private fun downloadAudio(audioPath: String): ByteArray? {
        return try {
            val baseUrl = SettingsActivity.getServerUrl(this).replace("/transcribe", "")
            val url = "$baseUrl$audioPath"

            val request = Request.Builder()
                .url(url)
                .get()
                .build()

            val response = httpClient.newCall(request).execute()

            if (response.isSuccessful) {
                response.body?.bytes()
            } else {
                null
            }
        } catch (e: Exception) {
            Log.e(TAG, "Error downloading audio", e)
            null
        }
    }

    private fun sendAck(requestId: String) {
        coroutineScope.launch {
            try {
                withContext(Dispatchers.IO) {
                    val baseUrl = SettingsActivity.getServerUrl(this@MainActivity).replace("/transcribe", "")
                    val url = "$baseUrl/api/response/$requestId/ack"

                    val request = Request.Builder()
                        .url(url)
                        .post("".toByteArray().toRequestBody(null))
                        .build()

                    val response = httpClient.newCall(request).execute()
                    Log.d(TAG, "Ack sent for $requestId: ${response.code}")
                }
            } catch (e: Exception) {
                Log.e(TAG, "Error sending ack", e)
            }
        }
    }

    private fun showNotification(message: String) {
        val shortMessage = if (message.length > 100) message.take(100) + "..." else message

        val builder = NotificationCompat.Builder(this, CHANNEL_ID)
            .setSmallIcon(android.R.drawable.ic_dialog_info)
            .setContentTitle("Claude")
            .setContentText(shortMessage)
            .setStyle(NotificationCompat.BigTextStyle().bigText(message))
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setAutoCancel(true)
            .setVibrate(longArrayOf(0, 200, 100, 200))

        try {
            NotificationManagerCompat.from(this).notify(NOTIFICATION_ID, builder.build())
        } catch (e: SecurityException) {
            Log.e(TAG, "No notification permission", e)
            showStatus(shortMessage)
        }
    }

    private suspend fun sendToServer(file: File): Result<String> {
        return try {
            val serverUrl = SettingsActivity.getServerUrl(this@MainActivity)
            val requestBody = file.asRequestBody("audio/mp4".toMediaType())

            val request = Request.Builder()
                .url(serverUrl)
                .post(requestBody)
                .build()

            val response = httpClient.newCall(request).execute()

            if (response.isSuccessful) {
                val body = response.body?.string() ?: ""
                Log.d(TAG, "Server response: $body")
                Result.success(body)
            } else {
                val error = "Server error: ${response.code}"
                Log.e(TAG, error)
                Result.failure(IOException(error))
            }
        } catch (e: Exception) {
            Log.e(TAG, "Network error", e)
            Result.failure(e)
        }
    }

    private fun cleanupRecording() {
        mediaRecorder?.release()
        mediaRecorder = null
        audioFile?.delete()
        audioFile = null
        isRecording = false
        updateUIState()
    }

    private fun updateUIState() {
        when {
            isPlayingAudio -> {
                // Audio playback state
                recordButton.visibility = View.GONE
                abortButton.visibility = View.GONE
                audioControls.visibility = View.VISIBLE
                doneButton.visibility = View.GONE  // Will show after audio completes
                progressBar.visibility = View.GONE
            }
            isWaitingForResponse -> {
                // Waiting for response state
                recordButton.visibility = View.GONE
                abortButton.visibility = View.VISIBLE
                audioControls.visibility = View.GONE
                doneButton.visibility = View.GONE
                progressBar.visibility = View.VISIBLE
            }
            isRecording -> {
                // Recording state
                recordButton.visibility = View.VISIBLE
                recordButton.text = "Stop & Send"
                recordButton.setBackgroundColor(ContextCompat.getColor(this, android.R.color.holo_red_dark))
                abortButton.visibility = View.GONE
                audioControls.visibility = View.GONE
                doneButton.visibility = View.GONE
                progressBar.visibility = View.GONE
            }
            else -> {
                // Normal/idle state
                recordButton.visibility = View.VISIBLE
                recordButton.text = "Record"
                recordButton.setBackgroundColor(ContextCompat.getColor(this, android.R.color.holo_blue_dark))
                abortButton.visibility = View.GONE
                audioControls.visibility = View.GONE
                doneButton.visibility = View.GONE
                progressBar.visibility = View.GONE
            }
        }
    }

    private fun showStatus(message: String, isError: Boolean = false) {
        statusText.text = message
        statusText.setTextColor(
            if (isError)
                ContextCompat.getColor(this, android.R.color.holo_red_light)
            else
                ContextCompat.getColor(this, android.R.color.white)
        )
    }

    private fun vibrate(duration: Long) {
        val vibrator = getSystemService(Vibrator::class.java)
        vibrator?.vibrate(VibrationEffect.createOneShot(duration, VibrationEffect.DEFAULT_AMPLITUDE))
    }

    private fun vibrate(pattern: LongArray) {
        val vibrator = getSystemService(Vibrator::class.java)
        vibrator?.vibrate(VibrationEffect.createWaveform(pattern, -1))
    }

    private fun acquireWakeLock() {
        if (wakeLock == null) {
            val powerManager = getSystemService(PowerManager::class.java)
            wakeLock = powerManager.newWakeLock(
                PowerManager.PARTIAL_WAKE_LOCK,
                "ClaudeWatch::ResponsePolling"
            )
        }
        wakeLock?.let {
            if (!it.isHeld) {
                it.acquire(3 * 60 * 1000L) // 3 minute timeout
                Log.d(TAG, "Wake lock acquired")
            }
        }
    }

    private fun releaseWakeLock() {
        wakeLock?.let {
            if (it.isHeld) {
                it.release()
                Log.d(TAG, "Wake lock released")
            }
        }
    }
}
