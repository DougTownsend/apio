module main(
    input clk,
    output reg strobe,
    output reg data,
    output reg cp
);

    reg [13:0] timer;      // Timer for counting to 1000 (1 second at 1kHz)
    reg [3:0] state;       // State machine
    reg [2:0] shift_count; // Count of shifts (0-7)
    reg [2:0] current_bit; // Which bit position is on (0-7)

    initial begin
        strobe = 0;
        data = 0;
        cp = 0;
        timer = 0;
        state = 0;
        shift_count = 0;
        current_bit = 0;
    end

    always @(posedge clk) begin
        case (state)
            4'd0: begin  // Waiting for 1 second
                strobe <= 0;
                cp <= 0;
                data <= 0;

                if (timer == 14'd999) begin
                    timer <= 14'd0;
                    state <= 4'd1;  // Start shifting
                    shift_count <= 3'd0;
                end else begin
                    timer <= timer + 14'd1;
                end
            end

            4'd1: begin  // Set data for this bit
                // Shift in a 1 if this is the target bit position, else 0
                if (shift_count == current_bit) begin
                    data <= 1;
                end else begin
                    data <= 0;
                end
                cp <= 0;
                state <= 4'd2;
            end

            4'd2: begin  // Clock pulse (CP high)
                cp <= 1;
                state <= 4'd3;
            end

            4'd3: begin  // Clock return to 0
                cp <= 0;

                if (shift_count == 3'd7) begin
                    state <= 4'd4;  // Done shifting all 8 bits
                end else begin
                    shift_count <= shift_count + 3'd1;
                    state <= 4'd1;  // Shift next bit
                end
            end

            4'd4: begin  // Strobe/latch (Strobe high)
                strobe <= 1;
                state <= 4'd5;
            end

            4'd5: begin  // Strobe return to 0
                strobe <= 0;
                current_bit <= current_bit + 3'd1;  // Rotate to next position (wraps 0-7)
                state <= 4'd0;  // Back to waiting
            end
        endcase
    end

endmodule
