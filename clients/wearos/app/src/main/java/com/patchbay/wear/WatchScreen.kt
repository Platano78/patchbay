package com.patchbay.wear

import android.animation.ValueAnimator
import androidx.activity.compose.BackHandler
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.keyframes
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.focusable
import androidx.compose.foundation.gestures.scrollBy
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.defaultMinSize
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.BasicText
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.input.rotary.onRotaryScrollEvent
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.launch

private val BACK = Color(0xFF05070D)
private val FACE = Color(0xFF13243B)
private val ICE = Color(0xFFE8F7FF)
private val MUTED = Color(0xFFAFC5DB)
private val NAVY = Color(0xFF23364D)
private val READY = Color(0xFF77CFFF)
private val LISTENING = Color(0xFF50E5C2)
private val WAITING = Color(0xFFC29BFF)
private val SPEAKING = Color(0xFF6DB7FF)
private val ERROR = Color(0xFFFF9D94)

private enum class Page { HOME, REPLY, SETTINGS }

private fun accent(phase: WatchPhase): Color = when (phase) {
    WatchPhase.LISTENING -> LISTENING
    WatchPhase.WAITING, WatchPhase.CONNECTING -> WAITING
    WatchPhase.SPEAKING -> SPEAKING
    WatchPhase.ERROR, WatchPhase.OFFLINE -> ERROR
    else -> READY
}

private fun headline(phase: WatchPhase): String = when (phase) {
    WatchPhase.UNCONFIGURED -> "Not configured"
    WatchPhase.CONNECTING -> "Connecting"
    WatchPhase.READY -> "Ready to talk"
    WatchPhase.LISTENING -> "Listening"
    WatchPhase.WAITING -> "Working"
    WatchPhase.SPEAKING -> "Speaking"
    WatchPhase.OFFLINE -> "Offline"
    WatchPhase.ERROR -> "Connection error"
}

/** Three views, not a scaled-down desktop cockpit. */
@Composable
fun WatchScreen(
    state: WatchUiState,
    onTalk: () -> Unit,
    onConfigure: (String) -> Unit,
    onRetry: () -> Unit
) {
    var page by rememberSaveable { mutableStateOf(Page.HOME) }
    BackHandler(enabled = page != Page.HOME) { page = Page.HOME }
    when (page) {
        Page.HOME -> HomeFace(
            state,
            onFaceTap = {
                when (state.phase) {
                    WatchPhase.READY, WatchPhase.LISTENING -> onTalk()
                    WatchPhase.ERROR, WatchPhase.OFFLINE -> onRetry()
                    WatchPhase.UNCONFIGURED -> page = Page.SETTINGS
                    else -> Unit
                }
            },
            onReply = { page = Page.REPLY },
            onSettings = { page = Page.SETTINGS }
        )
        Page.REPLY -> ReplyPage(state, onBack = { page = Page.HOME })
        Page.SETTINGS -> SettingsPage(
            state.endpoint,
            onSave = { entered -> onConfigure(entered); page = Page.HOME },
            onBack = { page = Page.HOME }
        )
    }
}

@Composable
private fun HomeFace(
    state: WatchUiState,
    onFaceTap: () -> Unit,
    onReply: () -> Unit,
    onSettings: () -> Unit
) {
    BoxWithConstraints(Modifier.fillMaxSize().background(BACK)) {
        val diameter = minOf(maxWidth, maxHeight)
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(horizontal = diameter * 0.12f, vertical = 20.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
            verticalArrangement = Arrangement.SpaceBetween
        ) {
            Column(horizontalAlignment = Alignment.CenterHorizontally) {
                SmallText("PATCHBAY", 13, ICE, FontWeight.Bold)
                SmallText(if (state.connected) "CONNECTED" else "NOT CONNECTED",
                    10, if (state.connected) LISTENING else MUTED)
            }

            Face(
                phase = state.phase,
                amplitude = state.audioLevel,
                modifier = Modifier.size(diameter * 0.56f),
                onTap = onFaceTap
            )
            Column(horizontalAlignment = Alignment.CenterHorizontally) {
                SmallText(headline(state.phase), 15, ICE, FontWeight.SemiBold)
                SmallText(state.detail, 11, MUTED, maxLines = 2)
            }
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Action("Reply", enabled = state.lastReply.isNotBlank(), onClick = onReply)
                Action("Settings", onClick = onSettings)
            }
        }
    }
}

@Composable
private fun Face(
    phase: WatchPhase,
    amplitude: Float,
    modifier: Modifier = Modifier,
    onTap: () -> Unit
) {
    val animated = ValueAnimator.areAnimatorsEnabled()
    val blinkTransition = rememberInfiniteTransition(label = "face blink")
    val eyesOpen by blinkTransition.animateFloat(
        initialValue = 1f, targetValue = 1f,
        animationSpec = infiniteRepeatable(
            animation = keyframes {
                durationMillis = 4200
                1f at 0
                1f at 3490
                0.06f at 3590
                1f at 3700
                1f at 4200
            },
            repeatMode = RepeatMode.Restart
        ), label = "eyes"
    )

    val ringColor = accent(phase)
    val clickLabel = when (phase) {
        WatchPhase.READY -> "Start talking to PatchBay"
        WatchPhase.LISTENING -> "Finish speaking and send to PatchBay"
        WatchPhase.OFFLINE, WatchPhase.ERROR -> "Reconnect to PatchBay"
        WatchPhase.UNCONFIGURED -> "Configure PatchBay connection"
        else -> headline(phase)
    }
    val tappable = phase in setOf(
        WatchPhase.READY, WatchPhase.LISTENING,
        WatchPhase.OFFLINE, WatchPhase.ERROR, WatchPhase.UNCONFIGURED
    )

    Canvas(
        modifier
            .clip(CircleShape)
            .clickable(enabled = tappable, role = Role.Button, onClick = onTap)
            .semantics { contentDescription = clickLabel }
    ) {
        val d = size.minDimension
        val cx = size.width / 2
        val cy = size.height / 2
        val eyeHeight = d * 0.083f * if (animated) eyesOpen else 1f
        drawCircle(FACE, radius = d * 0.485f)
        drawCircle(ringColor, radius = d * 0.472f,
            style = Stroke(width = d * 0.023f))
        drawCircle(Color(0xFF193149), radius = d * 0.395f)
        val eyeWidth = d * 0.115f
        for (dx in listOf(-0.15f, 0.15f)) {
            drawRoundRect(ICE,
                topLeft = Offset(cx + d * dx - eyeWidth / 2, cy - d * 0.105f),
                size = Size(eyeWidth, eyeHeight.coerceAtLeast(d * 0.009f)),
                cornerRadius = CornerRadius(d * 0.055f, d * 0.055f))
            if (eyeHeight > d * 0.05f) {
                drawCircle(Color(0xFF183553), radius = d * 0.023f,
                    center = Offset(cx + d * dx, cy - d * 0.065f))
            }
        }
        if (phase == WatchPhase.SPEAKING || phase == WatchPhase.LISTENING) {
            val mouthHeight = d * (0.045f + amplitude * 0.15f)
            drawOval(
                Color(0xFF07101C),
                topLeft = Offset(cx - d * 0.065f, cy + d * 0.065f),
                size = Size(d * 0.13f, mouthHeight)
            )
            drawOval(
                ICE,
                topLeft = Offset(cx - d * 0.065f, cy + d * 0.065f),
                size = Size(d * 0.13f, mouthHeight),
                style = Stroke(width = d * 0.012f)
            )
        } else if (phase == WatchPhase.WAITING || phase == WatchPhase.CONNECTING) {
            for (i in -1..1) {
                drawCircle(WAITING, radius = d * 0.017f,
                    center = Offset(cx + i * d * 0.07f, cy + d * 0.15f))
            }
        } else {
            drawArc(
                color = ICE, startAngle = 10f, sweepAngle = 160f,
                useCenter = false,
                topLeft = Offset(cx - d * 0.12f, cy + d * 0.045f),
                size = Size(d * 0.24f, d * 0.17f),
                style = Stroke(width = d * 0.018f)
            )
        }
        drawCircle(ringColor, radius = d * 0.019f,
            center = Offset(cx, cy - d * 0.435f))
    }
}

@Composable
private fun ReplyPage(state: WatchUiState, onBack: () -> Unit) {
    val scroll = rememberScrollState()
    val focusRequester = remember { FocusRequester() }
    val scope = rememberCoroutineScope()
    LaunchedEffect(Unit) { focusRequester.requestFocus() }

    Column(
        modifier = Modifier
            .fillMaxSize()
            .background(BACK)
            .verticalScroll(scroll)
            .onRotaryScrollEvent {
                scope.launch { scroll.scrollBy(it.verticalScrollPixels) }
                true
            }
            .focusRequester(focusRequester)
            .focusable()
            .padding(horizontal = 39.dp, vertical = 20.dp),
        horizontalAlignment = Alignment.CenterHorizontally
    ) {
        SmallText("LAST REPLY", 12, READY, FontWeight.Bold)
        Spacer(Modifier.height(12.dp))
        SmallText(state.lastReply.ifBlank { "No reply yet" },
            14, ICE, textAlign = TextAlign.Start)
        Spacer(Modifier.height(18.dp))
        Action("Back", onClick = onBack)
    }
}

@Composable
private fun SettingsPage(
    current: String,
    onSave: (String) -> Unit,
    onBack: () -> Unit
) {
    var input by rememberSaveable(current) { mutableStateOf(current) }
    Column(
        modifier = Modifier
            .fillMaxSize()
            .background(BACK)
            .verticalScroll(rememberScrollState())
            .padding(horizontal = 38.dp, vertical = 20.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.spacedBy(12.dp)
    ) {
        SmallText("PATCHBAY SERVER", 13, ICE, FontWeight.Bold)
        SmallText("Enter your trusted home WebSocket address", 11, MUTED)
        BasicTextField(
            value = input,
            onValueChange = { input = it.take(180) },
            singleLine = true,
            textStyle = TextStyle(color = ICE, fontSize = 13.sp),
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri),
            modifier = Modifier
                .fillMaxWidth()
                .clip(RoundedCornerShape(12.dp))
                .background(NAVY)
                .defaultMinSize(minHeight = 48.dp)
                .padding(9.dp)
                .semantics { contentDescription = "PatchBay WebSocket URL" },
            decorationBox = { inner ->
                Box {
                    if (input.isEmpty()) SmallText("ws://192.168.x.x:8765", 11, MUTED)
                    inner()
                }
            }
        )
        SmallText("Unencrypted ws:// is limited to private LAN IPs. Use wss:// only through a protected private connection.", 10, MUTED)
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Action("Back", onClick = onBack)
            Action("Save", onClick = { onSave(input) })
        }
    }
}

@Composable
private fun Action(label: String, enabled: Boolean = true, onClick: () -> Unit) {
    Box(
        modifier = Modifier
            .defaultMinSize(minWidth = 74.dp, minHeight = 44.dp)
            .clip(RoundedCornerShape(28.dp))
            .background(if (enabled) NAVY else Color(0xFF161E29))
            .clickable(enabled = enabled, role = Role.Button, onClick = onClick)
            .semantics { contentDescription = label },
        contentAlignment = Alignment.Center
    ) {
        SmallText(label, 12, if (enabled) ICE else MUTED, FontWeight.Medium)
    }
}

@Composable
private fun SmallText(
    text: String,
    size: Int,
    color: Color,
    weight: FontWeight = FontWeight.Normal,
    maxLines: Int = Int.MAX_VALUE,
    textAlign: TextAlign = TextAlign.Center
) {
    BasicText(
        text,
        style = TextStyle(
            color = color, fontSize = size.sp,
            fontWeight = weight, textAlign = textAlign
        ),
        maxLines = maxLines
    )
}
