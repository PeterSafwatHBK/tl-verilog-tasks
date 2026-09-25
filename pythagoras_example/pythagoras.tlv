\m5_TLV_version 1d: tl-x.org
\SV
   module pythagoras(input wire clk,
                     input wire [3:0] a_in,
                     input wire [3:0] b_in,
                     output wire [8:0] cc_sq_out);
\TLV
   |calc
      @1
         $aa[3:0] = *a_in;
         $bb[3:0] = *b_in;
         $aa_sq[7:0] = $aa ** 2;
         $bb_sq[7:0] = $bb ** 2;
      @2
         $cc_sq[8:0] = $aa_sq + $bb_sq;
         *cc_sq_out = $cc_sq;
\SV
   endmodule
