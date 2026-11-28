package com.patchbay.wear

import android.Manifest
import android.content.pm.PackageManager
import android.os.Bundle
import android.view.WindowManager
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.core.content.ContextCompat

/** Wear OS app, not a phone app. PatchBay remains the one and only agent. */
class MainActivity : ComponentActivity() {
    private val controller by lazy { WatchController(applicationContext) }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent { Root() }
    }

    override fun onStart() {
        super.onStart()
        controller.onForeground()
    }

    override fun onStop() {
        controller.onBackground() // no background microphone, socket, or playback
        super.onStop()
    }

    override fun onDestroy() {
        controller.dispose()
        super.onDestroy()
    }

    @Composable
    private fun Root() {
        val state by controller.ui.collectAsState()
        val permission = rememberLauncherForActivityResult(
            ActivityResultContracts.RequestPermission()
        ) { granted ->
            if (granted) controller.toggleTalk()
            else controller.permissionDenied()
        }

        // A user-initiated voice turn should not be silently cut off by wrist-screen timeout.
        // No always-on wakelock or service after the activity is hidden.
        DisposableEffect(state.phase) {
            if (state.phase in setOf(
                    WatchPhase.LISTENING, WatchPhase.WAITING, WatchPhase.SPEAKING
                )) {
                window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
            } else {
                window.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
            }
            onDispose { window.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON) }
        }

        WatchScreen(
            state = state,
            onTalk = {
                if (state.phase == WatchPhase.LISTENING ||
                    ContextCompat.checkSelfPermission(
                        this, Manifest.permission.RECORD_AUDIO
                    ) == PackageManager.PERMISSION_GRANTED
                ) controller.toggleTalk()
                else permission.launch(Manifest.permission.RECORD_AUDIO)
            },
            onConfigure = controller::configure,
            onRetry = controller::retry
        )
    }
}
