package com.claudewatch.companion.relay

import android.util.Log
import com.claudewatch.companion.SettingsActivity
import com.google.android.gms.wearable.*
import kotlinx.coroutines.*
import kotlinx.coroutines.tasks.await
import okhttp3.*
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.util.concurrent.TimeUnit

/**
 * WearableListenerService on the phone that receives relay requests from the watch
 * and forwards them to the server via direct HTTP/WebSocket connections.
 */
class PhoneRelayService : WearableListenerService() {

    companion object {
        private const val TAG = "PhoneRelaySvc"

        // Paths from watch
        private const val PATH_WS_CONNECT = "/relay/ws/connect"
        private const val PATH_WS_DISCONNECT = "/relay/ws/disconnect"
        private const val PATH_HTTP_REQUEST = "/relay/http/request"
        private const val PATH_AUDIO_UPLOAD = "/relay/audio/upload"
        private const val PATH_AUDIO_DOWNLOAD = "/relay/audio/download"

        // Paths to watch
        private const val PATH_HTTP_RESPONSE = "/relay/http/response"
        private const val PATH_AUDIO_UPLOAD_RESPONSE = "/relay/audio/upload/response"

        // Static so they survive service restarts
        private val httpClient = OkHttpClient.Builder()
            .connectTimeout(30, TimeUnit.SECONDS)
            .writeTimeout(60, TimeUnit.SECONDS)
            .readTimeout(30, TimeUnit.SECONDS)
            .build()

        // Pending audio uploads: requestId -> metadata
        private val pendingAudioMeta = java.util.concurrent.ConcurrentHashMap<String, JSONObject>()
    }

    private val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())

    override fun onMessageReceived(messageEvent: MessageEvent) {
        Log.d(TAG, "Message from watch: ${messageEvent.path}")

        when (messageEvent.path) {
            PATH_WS_CONNECT -> handleWsConnect(messageEvent.sourceNodeId)
            PATH_WS_DISCONNECT -> handleWsDisconnect()
            PATH_HTTP_REQUEST -> handleHttpRequest(messageEvent.sourceNodeId, messageEvent.data)
            PATH_AUDIO_UPLOAD -> handleAudioUploadMeta(messageEvent.data)
            PATH_AUDIO_DOWNLOAD -> handleAudioDownload(messageEvent.sourceNodeId, messageEvent.data)
        }
    }

    // --- WebSocket relay ---

    private fun handleWsConnect(watchNodeId: String) {
        Log.i(TAG, "Watch requested WS connect")
        RelayWebSocketManager.connect(applicationContext, watchNodeId)
    }

    private fun handleWsDisconnect() {
        Log.i(TAG, "Watch requested WS disconnect")
        RelayWebSocketManager.disconnect()
    }

    // --- HTTP relay ---

    private fun handleHttpRequest(watchNodeId: String, data: ByteArray) {
        scope.launch {
            try {
                val requestJson = JSONObject(String(data))
                val requestId = requestJson.getString("request_id")
                val method = requestJson.getString("method")
                val path = requestJson.getString("path")
                val body = requestJson.optString("body", "")
                val headersJson = requestJson.optJSONObject("headers")

                val baseUrl = getHttpBaseUrl()
                val url = "$baseUrl$path"

                Log.d(TAG, "Relaying HTTP: $method $url (id=$requestId)")

                val requestBuilder = Request.Builder().url(url)

                // Add headers
                if (headersJson != null) {
                    val keys = headersJson.keys()
                    while (keys.hasNext()) {
                        val key = keys.next()
                        requestBuilder.addHeader(key, headersJson.getString(key))
                    }
                }

                // Set method and body
                when (method.uppercase()) {
                    "GET" -> requestBuilder.get()
                    "POST" -> {
                        val contentType = headersJson?.optString("Content-Type", "application/json")
                            ?: "application/json"
                        val requestBody = body.toByteArray().toRequestBody(contentType.toMediaType())
                        requestBuilder.post(requestBody)
                    }
                    "PUT" -> {
                        val contentType = headersJson?.optString("Content-Type", "application/json")
                            ?: "application/json"
                        val requestBody = body.toByteArray().toRequestBody(contentType.toMediaType())
                        requestBuilder.put(requestBody)
                    }
                    "DELETE" -> requestBuilder.delete()
                }

                val response = httpClient.newCall(requestBuilder.build()).execute()
                val responseBody = response.body?.string() ?: ""

                val responseJson = JSONObject().apply {
                    put("request_id", requestId)
                    put("status", response.code)
                    put("body", responseBody)
                    put("success", response.isSuccessful)
                }

                Wearable.getMessageClient(applicationContext)
                    .sendMessage(watchNodeId, PATH_HTTP_RESPONSE, responseJson.toString().toByteArray())
                    .await()

                Log.d(TAG, "HTTP response sent to watch: $requestId (${response.code})")
            } catch (e: Exception) {
                Log.e(TAG, "HTTP relay error", e)
                // Send error response
                try {
                    val requestJson = JSONObject(String(data))
                    val requestId = requestJson.optString("request_id", "unknown")
                    val errorJson = JSONObject().apply {
                        put("request_id", requestId)
                        put("status", 0)
                        put("body", "Relay error: ${e.message}")
                        put("success", false)
                    }
                    Wearable.getMessageClient(applicationContext)
                        .sendMessage(watchNodeId, PATH_HTTP_RESPONSE, errorJson.toString().toByteArray())
                        .await()
                } catch (e2: Exception) {
                    Log.e(TAG, "Failed to send error response", e2)
                }
            }
        }
    }

    // --- Audio upload relay ---

    private fun handleAudioUploadMeta(data: ByteArray) {
        try {
            val meta = JSONObject(String(data))
            val requestId = meta.getString("request_id")
            pendingAudioMeta[requestId] = meta
            Log.d(TAG, "Audio upload meta received: $requestId (${meta.optInt("size")} bytes expected)")
        } catch (e: Exception) {
            Log.e(TAG, "Error parsing audio upload meta", e)
        }
    }

    override fun onChannelOpened(channel: ChannelClient.Channel) {
        val path = channel.path
        Log.d(TAG, "Channel opened: $path")

        if (path.startsWith("/relay/audio/upload/data/")) {
            val requestId = path.removePrefix("/relay/audio/upload/data/")
            scope.launch {
                handleAudioUploadData(channel, requestId)
            }
        }
    }

    private suspend fun handleAudioUploadData(channel: ChannelClient.Channel, requestId: String) {
        try {
            // Read audio data from channel
            val inputStream = Wearable.getChannelClient(applicationContext)
                .getInputStream(channel).await()

            val buffer = ByteArrayOutputStream()
            val chunk = ByteArray(8192)
            while (true) {
                val bytesRead = inputStream.read(chunk)
                if (bytesRead == -1) break
                buffer.write(chunk, 0, bytesRead)
            }
            inputStream.close()

            val audioBytes = buffer.toByteArray()
            Log.d(TAG, "Audio upload received: $requestId (${audioBytes.size} bytes)")

            // Get metadata
            val meta = pendingAudioMeta.remove(requestId)
            val responseMode = meta?.optString("response_mode", "text") ?: "text"

            // Forward to server
            val baseUrl = getHttpBaseUrl()
            val url = "$baseUrl/transcribe"

            val requestBody = audioBytes.toRequestBody("audio/mp4".toMediaType())
            val request = Request.Builder()
                .url(url)
                .header("X-Response-Mode", responseMode)
                .post(requestBody)
                .build()

            val response = httpClient.newCall(request).execute()
            val responseBody = response.body?.string() ?: ""

            Log.d(TAG, "Transcribe response: ${response.code} $responseBody")

            // Find the watch node to respond to
            val nodes = Wearable.getNodeClient(applicationContext).connectedNodes.await()
            val watchNode = nodes.firstOrNull() ?: run {
                Log.e(TAG, "No watch node to send audio upload response")
                return
            }

            val responseJson = JSONObject().apply {
                put("request_id", requestId)
                put("status", response.code)
                put("body", responseBody)
                put("success", response.isSuccessful)
            }

            Wearable.getMessageClient(applicationContext)
                .sendMessage(watchNode.id, PATH_AUDIO_UPLOAD_RESPONSE, responseJson.toString().toByteArray())
                .await()

            Log.d(TAG, "Audio upload response sent to watch: $requestId")
        } catch (e: Exception) {
            Log.e(TAG, "Audio upload relay error", e)
        }
    }

    // --- Audio download relay ---

    private fun handleAudioDownload(watchNodeId: String, data: ByteArray) {
        scope.launch {
            try {
                val requestJson = JSONObject(String(data))
                val requestId = requestJson.getString("request_id")
                val audioPath = requestJson.getString("path")

                val baseUrl = getHttpBaseUrl()
                val url = "$baseUrl$audioPath"

                Log.d(TAG, "Downloading audio for watch: $url (id=$requestId)")

                val request = Request.Builder().url(url).get().build()
                val response = httpClient.newCall(request).execute()

                if (response.isSuccessful) {
                    val audioBytes = response.body?.bytes() ?: byteArrayOf()
                    Log.d(TAG, "Audio downloaded: ${audioBytes.size} bytes, sending to watch via channel")

                    // Send via ChannelClient
                    val channel = Wearable.getChannelClient(applicationContext)
                        .openChannel(watchNodeId, "/relay/audio/download/data/$requestId")
                        .await()

                    val os = Wearable.getChannelClient(applicationContext)
                        .getOutputStream(channel).await()
                    os.write(audioBytes)
                    os.flush()
                    os.close()
                    Wearable.getChannelClient(applicationContext).close(channel).await()

                    Log.d(TAG, "Audio download sent to watch: $requestId")
                } else {
                    Log.e(TAG, "Audio download failed: ${response.code}")
                }
            } catch (e: Exception) {
                Log.e(TAG, "Audio download relay error", e)
            }
        }
    }

    // --- Helpers ---

    private fun getHttpBaseUrl(): String {
        val serverAddress = SettingsActivity.getServerAddress(applicationContext)
        // Server address is ip:5567 (WS port), HTTP is on 5566
        return "http://${serverAddress.replace(":5567", ":5566")}"
    }

    override fun onDestroy() {
        super.onDestroy()
        scope.cancel()
    }
}
