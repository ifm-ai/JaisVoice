import XCTest
@testable import HorizonVoice

final class TurnStateTests: XCTestCase {
    @MainActor func testNewTurnRejectsDelayedEvents() throws {
        let state = TurnState()
        let old = state.begin()
        let current = state.begin()
        state.accept(WireEvent(type: "response_delta", turn_id: old, text: "ancienne réponse"))
        XCTAssertEqual(state.response, "")
        state.accept(WireEvent(type: "response_delta", turn_id: current, text: "nouvelle réponse"))
        XCTAssertEqual(state.response, "nouvelle réponse")
    }

    @MainActor func testCancelledTurnCannotRestartFromLatePackets() {
        let state = TurnState(); let id = state.begin()
        state.cancel()
        state.accept(WireEvent(type: "correction", turn_id: id, text: "ne pas agir"))
        state.accept(WireEvent(type: "complete", turn_id: id, text: "trop tard"))
        XCTAssertEqual(state.cleaned, "")
        XCTAssertEqual(state.stage, "cancelled")
    }

    @MainActor func testToolLifecycleAndFinalResponse() {
        let state = TurnState(); let id = state.begin()
        state.accept(WireEvent(type: "transcript", turn_id: id, text: "Louvre Abu Dhabi non Paris"))
        state.accept(WireEvent(type: "correction", turn_id: id, text: "Louvre Paris"))
        state.accept(WireEvent(type: "tool_start", turn_id: id, tool_id: "one", tool_name: "browser_navigate"))
        state.accept(WireEvent(type: "tool_start", turn_id: id, tool_id: "one", tool_name: "browser_navigate"))
        XCTAssertEqual(state.actions.count, 1)
        state.accept(WireEvent(type: "tool_end", turn_id: id, tool_id: "one", result: "page"))
        state.accept(WireEvent(type: "response_delta", turn_id: id, text: "Bon"))
        state.accept(WireEvent(type: "response_delta", turn_id: id, text: "jour"))
        XCTAssertEqual(state.response, "Bonjour")
        state.accept(WireEvent(type: "complete", turn_id: id, text: "Bonjour !"))
        XCTAssertTrue(state.finished); XCTAssertTrue(state.actions[0].finished)
        XCTAssertEqual(state.response, "Bonjour !")
        XCTAssertEqual(state.raw, "Louvre Abu Dhabi non Paris")
    }

    func testDeletionDiffArabicKeepsFinalCorrection() {
        let diff = TranscriptDiff.words(original: "إلى أحمد لا إلى سارة", cleaned: "إلى سارة")
        XCTAssertEqual(diff.filter { !$0.removed }.map(\.text).joined(separator: " "), "إلى سارة")
        XCTAssertTrue(diff.contains(DiffWord(text: "أحمد", removed: true)))
        XCTAssertTrue(TranscriptDiff.isRTL("ابحث في باريس"))
        XCTAssertFalse(TranscriptDiff.isRTL("https://www.louvre.fr"))
    }

    @MainActor func testFailedToolDoesNotBecomeSuccess() {
        let state = TurnState(); let id = state.begin()
        state.accept(WireEvent(type: "tool_start", turn_id: id, tool_id: "one", tool_name: "browser_navigate"))
        state.accept(WireEvent(type: "tool_end", turn_id: id, tool_id: "one", result: "Page indisponible", error_code: "tool_error"))
        XCTAssertEqual(state.actions[0].errorCode, "tool_error")
        XCTAssertTrue(state.actions[0].finished)
    }

    func testDismissalPausesAndUsesRemainingTime() {
        var clock = DismissalClock(); let now = Date(timeIntervalSince1970: 0)
        clock.reset(at: now)
        XCTAssertFalse(clock.tick(at: now.addingTimeInterval(4), paused: false))
        XCTAssertEqual(clock.remaining, 6)
        XCTAssertFalse(clock.tick(at: now.addingTimeInterval(24), paused: true))
        XCTAssertEqual(clock.remaining, 6)
        XCTAssertFalse(clock.tick(at: now.addingTimeInterval(29), paused: false))
        XCTAssertTrue(clock.tick(at: now.addingTimeInterval(30), paused: false))
    }

    func testWAVHeaderAndClipping() {
        let data = WAVEncoder.encode(samples: [0, 1, -1, 3, .nan])
        XCTAssertEqual(data.count, 54)
        XCTAssertEqual(String(data: data.prefix(4), encoding: .ascii), "RIFF")
        XCTAssertEqual(Array(data[20..<24]), [1, 0, 1, 0])
        XCTAssertEqual(Array(data[24..<28]), [0x80, 0x3E, 0, 0])
        XCTAssertEqual(Array(data[34..<36]), [16, 0])
        XCTAssertEqual(Array(data[44..<54]), [0, 0, 255, 127, 1, 128, 255, 127, 0, 0])
    }

    func testWireEventParsesStructuredToolArguments() throws {
        let json = #"{"type":"tool_start","turn_id":"turn","tool_id":"tool","tool_name":"browser_navigate","arguments":{"url":"https://example.com","count":2,"enabled":true}}"#
        let event = try JSONDecoder().decode(WireEvent.self, from: Data(json.utf8))
        XCTAssertEqual(event.type, "tool_start")
        XCTAssertTrue(event.arguments?.pretty.contains("https://example.com") ?? false)
    }
}
