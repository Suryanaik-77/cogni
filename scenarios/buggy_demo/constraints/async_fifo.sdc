# SDC constraints for async_fifo dual-clock design
# Write clock domain: 100MHz
create_clock -name wr_clk -period 10.0 -waveform {0 5.0} [get_ports wr_clk]

# Read clock domain: 75MHz
create_clock -name rd_clk -period 13.33 -waveform {0 6.665} [get_ports rd_clk]

# Declare clocks as asynchronous (no phase relationship)
set_clock_groups -asynchronous \
    -group [get_clocks wr_clk] \
    -group [get_clocks rd_clk]

# False path: gray-coded pointer crossings are handled by 2-FF synchronizers
set_false_path -from [get_clocks wr_clk] -to [get_clocks rd_clk]
set_false_path -from [get_clocks rd_clk] -to [get_clocks wr_clk]

# I/O delays
set_input_delay  2.0 -clock [get_clocks wr_clk] -max [get_ports {wr_en wr_data}]
set_output_delay 1.5 -clock [get_clocks rd_clk] -max [get_ports {rd_data empty}]
set_output_delay 1.5 -clock [get_clocks wr_clk] -max [get_ports full]
